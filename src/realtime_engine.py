# ============================================================
# realtime_engine.py -- 盘中实时撮合引擎 (常驻)
# 模式:  盘中每 tick(默认15s)拉全A实时价(AKShare spot)
#        对目标持仓补仓到等权 / 触发止损卖出 / 跟买新进目标
#        每次tick写 live_state.json(供Web可视化读取) + 回写state.json
# 非交易时段: 引擎仍刷新价格快照(东财通常可返回收盘价), 不产生误撮合,
#             且 T+1 保护当日买入不卖。
# 用法:
#   python realtime_engine.py --once            # 单次运行(测试)
#   python realtime_engine.py --interval 15     # 常驻, 每15秒一个tick
#   python realtime_engine.py --no-intraday     # 非交易时段也刷新价格(演示)
# ============================================================

import os
import json
import sys
import time
import threading
import argparse
import traceback
from datetime import datetime, date, timedelta

import pandas as pd

# ---- 路径 ----
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
os.chdir(_BASE)

from config import (
    STATE_FILE, DAILY_DIR, DATA_DIR, INIT_CAPITAL, MAX_POS_RATIO,
    MAX_STOCKS, PAPER, SIGNAL_PARAMS, TRADE_BROKER,
)
from db import StockDB
from selector import RotationSelector, save_selection
from paper_book import PaperBook, PriceFeed

LIVE_STATE = os.path.join(DATA_DIR, "live_state.json")


def _keep_a_share(items: list) -> list:
    """按 A 股代码段过滤 top_n 列表(防御 selection/历史文件含可转债)."""
    if not items:
        return items
    kept = [it for it in items if _is_a_share_code(str(it.get("canon", "")))]
    return kept


def log(msg: str):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)


def _atomic_write_json(path: str, data: dict) -> None:
    """P9: 原子写入 JSON. 先写同目录临时文件再 os.replace 替换,
    避免进程中断时主文件被写坏(半截JSON).

    Windows 文件锁竞态: Dashboard 每 3s 轮询读取 live_state.json 时,
    os.replace 可能因文件被占用而触发 PermissionError.
    增加重试+退避 (最多 3 次, 50/100/200ms), 覆盖绝大部分读锁窗口.
    """
    import tempfile
    import time
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True) if d else None
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
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


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


# ---- A 股代码段校验 (与 build_factor_views.A_SHARE_CODE_FILTER 等价) ----
# daily_bars.symbol 为裸 6 位代码, symbols.name 未填充, 只能靠代码段辨识证券类型.
# 有效 A 股段: 深主板 000/001/002/003, 创业板 300/301/302,
#              沪主板 600/601/603/605, 科创板 688/689.
# 剔除: 沪市可转债 110/111/113, 深市可转债 123/127/128,
#       深B股 200, 沪B股 900, 北交所 920/8xx/4xx, 其它非A段.
_A_SHARE_PREFIXES = {
    "000", "001", "002", "003",
    "300", "301", "302",
    "600", "601", "603", "605",
    "688", "689",
}


def _is_a_share_code(raw: str) -> bool:
    """判断裸 6 位代码(或带 .SH/.SZ/.BJ 后缀)是否为 A 股."""
    if not raw:
        return False
    code = str(raw).split(".")[0]
    if len(code) < 3:
        return False
    return code[:3] in _A_SHARE_PREFIXES


def _prev_trade_day(day: str, db=None) -> "datetime|None":
    """返回严格早于 day 的最近一个交易日(基于 daily_bars 实际数据).
    相比 day-1 日历日, 能正确处理跨周末/法定节假日.

    [m4] DuckDB 退役后自动回退 h5i daily_bars (trading_days), 保证无 duck
    环境下回测/实盘对"前一交易日"的判定与有 duck 时一致.
    """
    close = db is None
    try:
        if db is None:
            db = StockDB()
        # 1) DuckDB 对照源(显式 duck 模式或文件仍在)
        try:
            from db import duck_available
            if duck_available():
                rows = db._conn().execute(
                    "SELECT DISTINCT date FROM daily_bars WHERE date < ? "
                    "ORDER BY date DESC LIMIT 1", [day]).fetchall()
                if rows and rows[0][0] is not None:
                    return rows[0][0]
        except Exception:
            pass
        # 2) h5i 主源 (DuckDB 已退役/读失败)
        try:
            from h5i_bar_store import H5iBarStore
            s = H5iBarStore()
            try:
                days = [x for x in s.trading_days() if x < str(day)[:10]]
            finally:
                s.close()
            if days:
                last = days[-1]
                try:
                    return pd.Timestamp(last).to_pydatetime()
                except Exception:
                    return datetime.strptime(str(last)[:10], "%Y-%m-%d")
        except Exception:
            return None
        return None
    except Exception:
        return None
    finally:
        try:
            if close and db is not None:
                db.close()
        except Exception:
            pass


def _plan_is_formal(plan: dict, day: str, d: str,
                    prev_trade_day: "datetime|None" = None) -> tuple[bool, str]:
    """校验 target_plan 是否为"盘后正式"产物.

    规则(双保险, 任一不满足即拒绝):
      a) top_n 里至少保留 1 个 A 股代码段; 若 DRL universe 混入可转债/B股等
         (历史 plan 的顶级缺陷), 视为退化残缺 plan, 拒绝.
      b) generated_at 时间戳必须落在「前一交易日 16:00 ~ 消费日 00:00]」区间.
         正式 plan 应基于前一交易日的完整收盘数据, 在前一交易日盘后 16:00 之后、
         消费日盘前生成. 消费日当天凌晨(如 08-28 02:28)/盘中异常重跑写出的
         过早或断续数据 plan 属非正式产物, 拒绝.
         边界: 若 generated_at 缺失则保守拒绝.

    Returns
    -------
    (ok, reason). ok=True 表示可消费.
    """
    items = plan.get("top_n") or []
    a_items = [it for it in items if _is_a_share_code(str(it.get("canon", "")))]
    if not a_items:
        return False, "top_n 无 A 股代码段 (疑似混入可转债/B股退化 plan)"
    gen = plan.get("generated_at")
    if not gen:
        return False, "缺少 generated_at 时间戳, 无法确认盘后正式性"
    try:
        gen_dt = datetime.strptime(str(gen), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return False, f"generated_at 无法解析: {gen}"
    # 前一交易日整日; 正式生成窗口 = [前一交易日 16:00, 消费日 00:00)
    day_dt = datetime.strptime(day, "%Y-%m-%d")
    # P8: 优先用传入的实际交易日(跨周末/节假日正确), 无则回退日历日前一天
    if prev_trade_day is not None:
        prev_day = pd.Timestamp(prev_trade_day).to_pydatetime()
    else:
        prev_day = day_dt - timedelta(days=1)
    formal_lower = prev_day.replace(hour=16, minute=0, second=0)
    formal_upper = day_dt.replace(hour=0, minute=0, second=0)
    if not (formal_lower <= gen_dt < formal_upper):
        return False, (f"generated_at {gen} 不在正式窗口 "
                       f"[{formal_lower:%Y-%m-%d %H:%M:%S}, {formal_upper:%Y-%m-%d %H:%M:%S})")
    return True, "ok"


def _plan_to_targets(plan: dict, day: str, d: str):
    """把校验通过的 DRL plan 转为 selector 兼容格式, 返回 (top_n, info).
    plan 已通过 _plan_is_formal 与 A 股代码段过滤.
    """
    items = plan.get("top_n") or []
    # 剔除历史残留的非 A 股项 (可转债/B股等), 防御旧文件含债券
    a_items = [it for it in items if _is_a_share_code(str(it.get("canon", "")))]
    if not a_items:
        log("target_plan 全为非A股, 弃用 plan")
        return None, None
    top_n = []
    for it in a_items:
        top_n.append({
            "canon": it["canon"],
            "price": it.get("price"),
            "score": it.get("drl_score", 0.0),
            "target_weight": it.get("target_weight"),
            "source": "drl_plan",
            "change_pct": it.get("change_pct"),
            "turnover": it.get("turnover"),
            "source_signal": it.get("source_signal"),
        })
    info = {
        "date": day,
        "source": "drl_plan",
        "method": plan.get("method"),
        "weights_used": plan.get("weights_used"),
        "universe_size": plan.get("universe_size"),
        "generated_at": plan.get("generated_at"),
        "top_n": top_n,
    }
    return top_n, info


def _try_load_daily_plan(d: str, day: str, prev_trade_day=None):
    """读 data/drl/<d>/target_plan.json 并做正式性校验与 A 股过滤.

    d: 目录代码(YYYYMMDD); day: 消费日(YYYY-MM-DD).
    prev_trade_day: 可选, 消费日的前一实际交易日(跨周末/节假日正确).
    返回 (top_n, info) 或 (None, None).
    """
    plan_path = os.path.join(DATA_DIR, "drl", d, "target_plan.json")
    if not os.path.exists(plan_path):
        return None, None
    try:
        with open(plan_path, encoding="utf-8") as f:
            plan = json.load(f)
    except Exception:
        return None, None
    if not isinstance(plan, dict):
        return None, None
    ok, reason = _plan_is_formal(plan, day, d, prev_trade_day=prev_trade_day)
    if not ok:
        log(f"drl/{d} target_plan 校验未过, 跳过: {reason}")
        return None, None
    if not plan.get("top_n"):
        return None, None
    return _plan_to_targets(plan, day, d)


def load_targets(day: str):
    """从 DRL 目标计划 / 选股结果读取目标持仓 (回测与实盘共用的唯一入口).

    优先级 (全局统一, 回测/实盘走同一逻辑):
      1) data/drl/<YYYYMMDD>/target_plan.json  (当日目录, DRL 正式 plan)
         -> 消费前校验: 剔除非 A 股代码段 + 校验盘后正式生成时间戳
      2) data/daily/<YYYYMMDD>/selection.json  (当日选择)
      3) 跨日回退 - DRL: 按目录倒序遍历 data/drl/*, 取最近一个通过正式校验的
         盘后 plan (不依赖目录名==消费日, 天然对齐训练日/消费日错位)
      4) 跨日回退 - selection: 按目录倒序遍历 data/daily/*, 取最近 selection
      5) 现场选股

    返回 (target, sel_info, sel_day). sel_day 是目标池实际对应的选股日.
    target 项至少含 canon + price, DRL plan 会多带 drl_score / target_weight.
    """
    d = day.replace("-", "")

    # P8: 消费日的前一实际交易日(跨周末/节假日), 供 DRL plan 正式窗口校验使用
    prev_trade_day = _prev_trade_day(day)

    # 1) 优先: 当日目录 DRL plan (带 A股过滤 + 时间戳校验)
    top_n, info = _try_load_daily_plan(d, day, prev_trade_day=prev_trade_day)
    if top_n:
        return top_n, info, d

    # 2) 当日 selection.json
    sel_path = os.path.join(DAILY_DIR, d, "selection.json")
    if os.path.exists(sel_path):
        try:
            with open(sel_path, encoding="utf-8") as f:
                cand_sel = json.load(f)
            if cand_sel and cand_sel.get("top_n"):
                cand_sel["top_n"] = _keep_a_share(cand_sel["top_n"])
                if not cand_sel["top_n"]:
                    pass
                else:
                    for it in cand_sel["top_n"]:
                        it.setdefault("source", "selection")
                    return cand_sel["top_n"], cand_sel, d
        except Exception:
            pass

    # 3) 跨日回退 - DRL: 最近一个正式的盘后 plan (排除当日目录, 已被步骤1处理)
    try:
        drl_dirs = [x for x in os.listdir(os.path.join(DATA_DIR, "drl"))
                    if x.isdigit() and len(x) == 8]
        drl_dirs.sort(reverse=True)
        for cand in drl_dirs:
            if cand == d:
                continue
            top_n, info = _try_load_daily_plan(cand, day,
                                               prev_trade_day=prev_trade_day)
            if top_n:
                return top_n, info, cand
    except Exception:
        pass

    # 4) 跨日回退 - selection: 最近的 selection.json
    sel = None
    sel_day = d
    try:
        dirs = [x for x in os.listdir(DAILY_DIR)
                if os.path.isdir(os.path.join(DAILY_DIR, x)) and x.isdigit()]
        dirs.sort(reverse=True)
        for cand in dirs:
            sp = os.path.join(DAILY_DIR, cand, "selection.json")
            if os.path.exists(sp):
                try:
                    with open(sp, encoding="utf-8") as f:
                        cand_sel = json.load(f)
                    if "top_n" in cand_sel and cand_sel["top_n"]:
                        sel, sel_day = cand_sel, cand
                        break
                except Exception:
                    continue
    except Exception:
        pass
    if sel and "top_n" in sel:
        sel["top_n"] = _keep_a_share(sel.get("top_n", []))
        if not sel["top_n"]:
            sel = None
    if sel and "top_n" in sel:
        for it in sel.get("top_n", []):
            it.setdefault("source", "selection")
        return sel.get("top_n", []), sel, sel_day

    # 5) 现场选股
    db = StockDB()
    try:
        sel = RotationSelector(db, n=MAX_STOCKS).select()
        sel["date"] = day
        sel["source"] = "live_select"
        save_selection(sel, d)
    finally:
        db.close()
    for it in sel.get("top_n", []):
        it.setdefault("source", "live_select")
    return sel.get("top_n", []), sel, d


class RealtimeEngine:
    def __init__(self, interval: float = 15.0, intraday_only: bool = True):
        self.interval = interval
        self.push_only_in_session = intraday_only
        self.pb = PaperBook()
        self.pb.restore(load_state())           # 延续历史现金/持仓
        self.pb.trade_date = date.today().strftime("%Y-%m-%d")
        self.pb.day = self.pb.trade_date
        self.targets, self.sel, self.sel_day = load_targets(self.pb.trade_date)
        # 策略层优化: 目标权重与配置对齐(DRL 等权 plan -> 现算 fml 激活预测加权)
        try:
            from target_weighting import ensure_target_weights
            ensure_target_weights(self.targets, as_of=self.pb.trade_date)
        except Exception:
            pass
        self.cur_day = self.pb.trade_date          # 真实运行日 (写入目录用)
        self.feed = PriceFeed(cache_ttl=15)
        # ---- 午间重选状态 ----
        self.midday_done = False                  # 当日是否已执行午间重选
        self._resel_lock = threading.Lock()
        self.midday_result = None                 # 重选概况(供页面展示)
        self.midday_targets = None                # 午间重选后的目标池(重选完成前为None=沿用盘前)

        self.tick = 0
        self._ref_db_inst = None                  # DuckDB 参考价连接 (惰性创建, 复用至引擎退出)
        self._ca_applied_day = None               # P2: 最近已执行分红/除权入账的日期
        self._broker_logged = False               # 下单通道(模拟盘)一次性日志标记
        # ---- 降摩擦: 单日换手预算(进程内, 按交易日重置) ----
        self._to_day = self.pb.trade_date          # 当前预算归属的交易日
        self._to_used = 0.0                       # 当日策略调仓已用成交额(卖出+买入)金额
        # ---- 降摩擦: 策略调仓间隔(2026-09-04) ----
        # 策略换仓(离池卖出+补权买入)最小间隔自然日; 期间仅止损/风控动作.
        self._last_rebal_day = None               # 最近一次策略调仓日 (YYYY-MM-DD)
        # ---- 风控回补冷却(进程内, 按交易日重置) ----
        # 当日被风控减仓(止损/集中度压回)的标的, 免疫"立即平权补回"——
        # 否则下个 tick 会把刚压回的仓位按 band(9500) 买回, 又超集中度上限
        # (8000) 再次压回, 形成 压回↔补权 自振荡, 每次循环都烧双边手续费.
        # 这些标的当日不再回补, 次日自动解冻.
        self._cooled = set()              # 当日禁止回补的 canon 集合

    # ---------- 交易时段判定 (A股) ----------
    @staticmethod
    def in_session(now: datetime = None) -> bool:
        now = now or datetime.now()
        # 非交易日(周末+节假日)防御式跳过: 走权威交易日历, 防节假日误撮合/误估值
        try:
            from trading_calendar import is_trading_day as _tc_day
            if not _tc_day(now):
                return False
        except Exception:
            if now.weekday() >= 5:                # 回退: 周末非交易日
                return False
        hm = now.hour * 60 + now.minute
        am_open, am_close = 9 * 60 + 30, 11 * 60 + 30
        pm_open, pm_close = 13 * 60, 15 * 60
        return am_open <= hm <= am_close or pm_open <= hm <= pm_close

    # ---------- P2: 分红/除权入账 ----------
    def _apply_corporate_actions(self) -> None:
        """查询持仓标的当日及以前到期、且未入账的分红/送转事件并入账.

        在每日首次估值前调用一次; 已入账事件key记录在 pb.applied_corp 并持久化,
        跨日/重启不会重复记账. 入账后权益不变(送转平摊成本 / 红利入现金),
        但除权日不复权原始价导致的账面骤降会被抵消, 不再误触止损.
        """
        try:
            self._ca_applied_day = self.pb.trade_date
            if not self.pb.positions:
                return
            db = self._ref_db()
            as_of = date.today()
            # 只入账"建仓之后到期"的分红/送转, 避免把 1996~2026 全部历史事件
            # 在首日建仓时一次性入账 (qty 被历史送转放大, 假性收益暴涨).
            since_dates = {
                c: p.get("buy_date") for c, p in self.pb.positions.items()
                if p.get("buy_date")
            }
            events = db.corporate_actions_due(
                list(self.pb.positions.keys()), as_of, self.pb.applied_corp,
                since_dates)
            if not events:
                return
            booked = self.pb.apply_corporate_actions([
                {**ev, "symbol": ev["symbol"]} for ev in events])
            booked = [b for b in booked if b["canon"]]
            if booked:
                detail = "; ".join(
                    f"{b['canon']}:{b['type']}" for b in booked)
                log(f"[P2分红] {self.pb.trade_date} 入账 {len(booked)} 笔: {detail}")
        except Exception as e:
            log(f"[P2分红] 入账异常: {e}")
            import traceback; traceback.print_exc()

    # ---------- 一次 tick ----------
    def run_tick(self):
        self.tick += 1
        now = datetime.now()
        session = self.in_session(now)
        tgt_codes = [t["canon"] for t in self.targets]
        all_codes = list(dict.fromkeys(tgt_codes + list(self.pb.positions.keys())))
        # 同步持仓给 feed, 让 watchlist 路径生效 (避免每 tick 全A抓取)
        self.feed.set_positions(self.pb.positions)

        # 非交易时段且不允许盘中外拉取 -> 不拉实时行情, 仅用最近一个已知价刷新账面估值.
        # (避免每 15s 触发一次全A快照的额外负载; 收盘前半小时启动的引擎也要避免集合竞价前盲拉)
        if self.push_only_in_session and not session:
            latest = dict(self.pb.d_price) if hasattr(self.pb, "d_price") and self.pb.d_price \
                else dict.fromkeys(all_codes, None)
            live_src = "price_hold"      # 沿用现有账面价, 不动仓
            self._write_state(latest, session, live_src)
            return

        # 拉实时价 (交易时段必须; 非交易时段(允许off-sheet)也尝试拉取一次, 失败即跳过)
        latest = self.feed.get_latest(all_codes)
        errors = self.feed.last_error
        if errors:
            log(f"实时源告警: {errors[:120]}")
        live_src = "akshare_spot"
        # 兜底: 实时源断连/缺失价 -> 用 DuckDB 最近收盘价补齐, 保证撮合恒有价
        missing = [c for c in all_codes if c not in latest or not latest.get(c)]
        if missing or not latest:
            ref = self._disk_ref_prices(missing)
            if ref:
                for c, p in ref.items():
                    if p > 0 and not latest.get(c):
                        latest[c] = p
                        # 同步到 feed.quotes, 让 _tradable / 涨跌停判断有价可用
                        if c not in self.feed.quotes:
                            self.feed.quotes[c] = {"price": p, "last_close": p,
                                                   "limit_up": None, "limit_down": None,
                                                   "volume": 0, "suspended": False}
                live_src = "duckdb_reference"

        # 用实时价更新账面撮合价
        self.pb.d_price = {c: latest[c] for c in latest if latest.get(c) and latest[c] > 0}

        # P2: 每日一次把到期除权/分红事件入账, 防止除权日账面市值骤降出现虚假亏损.
        if self._ca_applied_day != self.pb.trade_date:
            self._apply_corporate_actions()

        # 只在交易时段撮合; 非交易时段只刷新估值不动仓
        if session:
            self._rebalance(latest)
        else:
            log(f"[{'交易时段' if session else '非交易时段'}] tick#{self.tick} 刷新价格, 不动仓")

        # ---- 午间重选触发 (11:28, 上午盘尚未结束, 拉到快照后下午按新池调仓) ----
        hm = now.hour * 60 + now.minute
        if (not self.midday_done) and now.weekday() < 5 and 11 * 60 + 20 <= hm <= 11 * 60 + 35:
            self.midday_done = True          # 立即标记避免重复触发 (线程内会再置)
            log("触发午间重选(独立线程)...")
            threading.Thread(target=self._midday_reselect, daemon=True).start()

        self._write_state(latest, session, live_src)

    # ---------- 参考价兜底 (实时源断连时用 DuckDB 最近收盘价) ----------
    def _ref_db(self) -> StockDB:
        """惰性创建并在引擎生命周期内复用 DuckDB 参考价连接, 避免每 tick 新建."""
        if self._ref_db_inst is None:
            self._ref_db_inst = StockDB()
        return self._ref_db_inst

    def close_ref_db(self):
        """收盘/退出时关闭复用的参考价连接."""
        if self._ref_db_inst is not None:
            try:
                self._ref_db_inst.close()
            except Exception:
                pass
            self._ref_db_inst = None

    def _disk_ref_prices(self, codes: list) -> dict:
        """从 DuckDB 取最近一根日线收盘价作参考价. codes为缺价标的."""
        if not codes:
            return {}
        out = {}
        db = self._ref_db()
        try:
            for c in codes:
                try:
                    df = db.get_bars(c, 1)
                    if df is not None and not df.empty:
                        px = float(df.iloc[-1]["close"])
                        if px > 0:
                            out[c] = px
                except Exception:
                    continue
        except Exception:
            pass
        return out

    # ---------- 可交易性判定 (A股: 涨停不能买 / 跌停不能卖 / 停牌跳过) ----------
    def _tradable(self, canon: str):
        """返回 (action_allowed, reason). 复用 feed.quotes(在get_latest时已刷新)."""
        q = self.feed.quotes.get(canon)
        if q is None:
            return True, ""                    # 无行情 -> 交由价格>0兜底
        if q.get("suspended"):
            return False, "停牌"
        px = q.get("price", 0)
        if px <= 0:
            return False, "无有效价"
        lu, ld = q.get("limit_up"), q.get("limit_down")
        if lu is not None and ld is not None:
            if px >= lu:
                return "sell_only", "涨停(不可买)"
            if px <= ld:
                return "buy_only", "跌停(不可卖)"
        return True, ""

    @staticmethod
    def _plan_signal_buy(t: dict) -> bool:
        """plan 目标信号是否允许多头开仓 (方向1/预期收益覆盖成本).

        仅当标签明确为非多头(SELL/减仓/观望/中性)时拦截; 无标签(BUY/None/
        selector 池)一律放行, 避免误杀只有 score 的目标.
        """
        s = str(t.get("source_signal") or "").strip().upper()
        if not s:
            return True
        neg = ("SELL" in s) or ("卖出" in s) or ("减仓" in s) or ("观望" in s) or ("中性" in s)
        return not neg

    def _compute_gate(self, equity_now: float) -> dict:
        """IC 门控 + 单日亏损防御 (2026-09-07).

        返回 {interval_days, exposure_mult, freeze_new_buys, regime, reasons}.
        不可用时返回"放行默认", 让原调仓节奏照常执行 (绝不让新机制阻塞交易).
        """
        try:
            from factor_gate import build_plan_from_cache
        except Exception:
            return {"regime": "unavailable", "exposure_mult": 1.0,
                    "freeze_new_buys": False,
                    "interval_days": int(PAPER.get("rebalance_interval_days", 0) or 0),
                    "reasons": []}
        # 单日亏损基线: 每个交易日首个 rebalance 时点记录日初权益
        if getattr(self, "_gate_day", None) != self.pb.trade_date:
            self._gate_day = self.pb.trade_date
            self._day_start_eq = equity_now
        daily_loss = 0.0
        if getattr(self, "_day_start_eq", 0.0) and self._day_start_eq > 0:
            daily_loss = equity_now / self._day_start_eq - 1.0
        try:
            base = int(PAPER.get("rebalance_interval_days", 3) or 3)
        except Exception:
            base = 3
        # 滞后状态 (risk 进入/退出各需连续3日) 在引擎生命周期内跨日持久
        if getattr(self, "_gate_hyst", None) is None:
            self._gate_hyst = {"risk_streak": 0, "exit_streak": 0, "in_risk": False}
        try:
            plan = build_plan_from_cache(daily_loss=daily_loss,
                                         base_interval=base,
                                         hyst=self._gate_hyst)
        except Exception as e:
            plan = {"regime": "unavailable", "exposure_mult": 1.0,
                    "freeze_new_buys": False, "interval_days": base,
                    "reasons": [f"门控异常: {type(e).__name__}: {e}"]}
        # 每个交易日只打一次完整日志 (盘中 tick 不再刷屏); 带 GATE_ACTIVE/INACTIVE
        # 标记供盘后检索 (路径A: 确认门控当日是否生效).
        if getattr(self, "_gate_logged_day", None) != self.pb.trade_date:
            self._gate_logged_day = self.pb.trade_date
            _active = plan.get("regime") not in ("unavailable", "disabled", "unknown")
            _tag = "GATE_ACTIVE" if _active else "GATE_INACTIVE"
            log(f"[{_tag}] 因子门控: regime={plan.get('regime')} "
                f"暴露={plan.get('exposure_mult')} "
                f"冻结新买={plan.get('freeze_new_buys')} "
                f"调仓间隔={plan.get('interval_days')}天 "
                f"当日盈亏={daily_loss * 100:.2f}%")
            for r in (plan.get("reasons") or [])[-3:]:
                log(f"  [{_tag}] 门控原因: {r}")
        return plan

    def _rebalance(self, latest: dict):
        """盘中补仓: 对目标持仓按等权目标市值补足; 目标组合外的持仓卖出.
        约束: 涨停不买 / 跌停不卖 / 停牌跳过 / 保留现金>=CASH_CUSHION.
        止损: 单持仓亏损>8% 强制卖出(可卖部分).
        下单通道: 固定走模拟盘 (PaperBook 纸面撮合). TRADE_BROKER 当前恒为
        "paper"; 若改成 "easytrader" 会因实盘通道未接入而拒绝下单, 绝不静默回退实盘.
        """
        if not self._broker_logged:
            self._broker_logged = True
            log(f"下单通道: TRADE_BROKER={TRADE_BROKER} -> PaperBook 模拟盘撮合(不连券商)")
        if TRADE_BROKER != "paper":
            log(f"止损: 实盘通道(easytrader)未接入, 拒绝下单 TRADE_BROKER={TRADE_BROKER}")
            return 0
        total_target_mv = INIT_CAPITAL * MAX_POS_RATIO
        n = max(len(self.targets), 1)
        band = total_target_mv / n            # 等权参考(缺省回退/日志)
        target_set = {t["canon"] for t in self.targets}
        # 策略层优化 (2026-09-05): 消费每项 target_weight(f_ml 预测加权,
        # 收缩+集中度封顶). target_mv_i = total_target_mv * w_i, 缺省项补等权,
        # 和归一 -> 等权(0.1/n10)恰好等于原 band, 完全向后兼容.
        from target_weighting import normalize_weights
        normalize_weights(self.targets)
        _tw_sum = sum(float(t.get("target_weight", 0.0) or 0.0)
                      for t in self.targets) or 1.0
        self._tw = {t["canon"]: float(t.get("target_weight", 0.0) or 0.0) /
                    _tw_sum for t in self.targets}

        min_cash = INIT_CAPITAL * (1 - MAX_POS_RATIO)   # 5%现金底线

        # 0) 单日换手预算(降摩擦): 策略调仓(卖出离池+买入补权)成交额合计不得超过
        #    净资产*max_turnover_pct. 跨交易日重置. 止损/组合风控不占用此预算.
        to_pct = float(PAPER.get("max_turnover_pct", 0.25) or 0.25)
        if self._to_day != self.pb.trade_date:
            self._to_day = self.pb.trade_date
            self._to_used = 0.0
            self._cooled = set()        # 跨交易日: 冷却集合解冻
        equity_now = self.pb.cash + self.pb.market_value()
        to_budget = equity_now * to_pct

        # ---- IC 门控 + 单日亏损防御 (2026-09-07) ----
        # 门控 plan 决定: 暴露系数(exposure_mult, 缩放买入目标市值)、是否冻结
        # 新买入、以及自适应调仓间隔. 仅影响策略调仓, 不影响止损/组合风控.
        self._gate = self._compute_gate(equity_now)
        _exp_mult = float(self._gate.get("exposure_mult", 1.0) or 1.0)

        # ---- 策略调仓间隔 gate (2026-09-04 降摩擦: 方向2/降频) ----
        # 距上次策略调仓不足 N 自然日 -> 本轮只做止损/风控, 不做策略换仓.
        # N 由 IC 门控自适应放大 (risk/caution 自动延后下次调仓).
        # 仅在实际发生策略成交时才推进 _last_rebal_day: 若某窗口无成交
        # (目标未变 / 涨停买不进), 不锁定, 后续 tick/日仍可无成本再试.
        rebal_iv = int(self._gate.get("interval_days")
                       or PAPER.get("rebalance_interval_days", 0) or 0)
        gate_open = True
        gap_days = 999
        if rebal_iv > 0 and self._last_rebal_day:
            try:
                gap_days = (date.today() - date.fromisoformat(self._last_rebal_day)).days
            except Exception:
                gap_days = 999
            gate_open = gap_days >= rebal_iv
        if not gate_open:
            log(f"调仓间隔未到(距上次{self._last_rebal_day} {gap_days}天<{rebal_iv}), 本轮仅止损/风控")

        # 1) 卖出目标组合外的持仓 (可卖部分; 跌停/停牌跳过)
        min_hold = int(PAPER.get("min_hold_days", 0) or 0)
        if gate_open:
            for canon in list(self.pb.positions.keys()):
                if canon not in target_set:
                    tb, reason = self._tradable(canon)
                    if tb is False or tb == "buy_only":
                        log(f"跳过卖出({canon}) {reason}")
                        continue
                    # 最小持仓天数: 持仓不足N天不出售(止损/风控除外)
                    if min_hold > 0:
                        buy_date = self.pb.positions[canon].get("buy_date")
                        if buy_date:
                            try:
                                held = (date.today() - date.fromisoformat(buy_date)).days
                            except Exception:
                                held = 999
                            if held < min_hold:
                                log(f"跳过卖出({canon}) 持仓不足{min_hold}天(已持{held}天)")
                                continue
                    pr = self.pb.market_price(canon)
                    if pr > 0:
                        qty = self.pb.positions[canon]["qty"]
                        mv = qty * pr
                        if self._to_used + mv > to_budget:   # 换手预算门控
                            log(f"跳过卖出({canon}) 超当日换手预算(已用{self._to_used:.0f}/{to_budget:.0f})")
                            continue
                        self.pb.sell(canon, qty, pr)
                        self._to_used += mv
                        self._cooled.add(canon)   # 任何卖出 → 当日不再回补(防振荡)
                        log(f"卖出({canon}) 离开目标池 @{pr:.3f}")

        # 2) 单票止损 (可卖部分; 跌停/停牌跳过). 阈值与 config.PAPER.stop_loss 统一 -> -3%.
        stop = PAPER.get("stop_loss", 0.03)
        for canon in list(self.pb.positions.keys()):
            p = self.pb.positions[canon]
            tb, reason = self._tradable(canon)
            if tb is False or tb == "buy_only":
                log(f"跳过止损({canon}) {reason}")
                continue
            pr = self.pb.market_price(canon)
            if pr <= 0:
                continue
            drawdown = pr / p["avg_cost"] - 1
            if drawdown < -stop:
                qty = p["qty"] - p.get("locked_qty", 0)   # 只卖可卖部分
                if qty > 0:
                    self.pb.sell(canon, qty, pr)
                    self._cooled.add(canon)   # 止损减仓 -> 当日不再回补
                    log(f"止损卖出({canon}) {drawdown*100:.1f}% @{pr:.3f}")

        # 2.1) 组合级风控: 熔断判定 + 集中度压回 (集中度压回由 PaperBook 兜底执行)
        risk = self.pb.apply_risk_controls()
        if risk["stopped"] or risk["trimmed"]:
            log(f"风控动作: 止损={risk['stopped']} 压回={risk['trimmed']} "
                f"组合回撤={risk['drawdown_pct']}% state={risk['state']}")
            # 风控减仓标的 -> 当日不再平权回补(反自振荡)
            for c in (list(risk["stopped"]) + list(risk["trimmed"])):
                if c:
                    self._cooled.add(c)
            log(f"冷却集更新: {sorted(self._cooled)} (共{len(self._cooled)}只)")
        if risk["state"] == "DRAW_DOWN":
            log(f"组合熔断: 回撤 {risk['drawdown_pct']}%, 暂停加仓 (仅止损/离场)")

        # 3) 补仓目标池到等权 (保留现金底线; 涨停/停牌跳过; 受调仓间隔 gate)
        #    组合熔断(DRAW_DOWN)或 IC 门控冻结/单日重亏时暂停一切加仓
        if self.pb.state == "DRAW_DOWN" or self._gate.get("freeze_new_buys"):
            if self._gate.get("freeze_new_buys") and self.pb.state != "DRAW_DOWN":
                log("门控冻结: 暂停新买入 (仅止损/风控离场)")
            return 0
        if gate_open:
            for t in self.targets:
                canon = t["canon"]
                if canon in self._cooled:
                    log(f"跳过买入({canon}) 当日风控减仓冷却(不回补)")
                    continue
                tb, reason = self._tradable(canon)
                if tb is False or tb == "sell_only":
                    log(f"跳过买入({canon}) {reason}")
                    continue
                # 信号方向门槛 (2026-09-04 方向1/预期收益覆盖成本):
                # plan 目标若明确非多头(source_signal=SELL/观望), 预期边际收益
                # 难以覆盖往返手续费, 不开新仓. 无标签不拦截(兼容 selector 池).
                if PAPER.get("min_signal_buy") and not self._plan_signal_buy(t):
                    log(f"跳过买入({canon}) 目标信号非多头(source_signal={t.get('source_signal')})")
                    continue
                pr = self.pb.market_price(canon)
                if pr <= 0:
                    continue
                cur_mv = 0
                if canon in self.pb.positions:
                    cur_mv = self.pb.positions[canon]["qty"] * pr
                # 目标市值 = 总投资预算(INIT*MAX_POS) * 该项 target_weight (2026-09-05)
                # IC 门控暴露系数缩放: caution/risk 档仅按比例补权, 余留现金 (2026-09-07)
                tw = getattr(self, "_tw", {}).get(canon)
                target_mv = (total_target_mv * float(tw) if tw else band) * _exp_mult
                diff = target_mv - cur_mv
                if diff <= pr * 100:
                    continue
                qty = int(diff // (pr * 100)) * 100
                if qty < 100:
                    continue
                # 现金硬约束: 买入后现金须>=min_cash (成本口径与 PaperBook 撮合价一致: 滑点+冲击)
                impact = PAPER.get("impact_cost", 0.0002)
                exec_ratio = (1 + PAPER["slippage"] + impact)
                cost = qty * pr * exec_ratio
                fee = self.pb._commission(cost, is_sell=False)
                if self.pb.cash - cost - fee < min_cash:
                    # 缩档到现金底线允许的最大整手
                    budget = self.pb.cash - min_cash
                    avail_qty = int(budget // (pr * exec_ratio * 100)) * 100
                    qty = min(qty, avail_qty)
                if qty >= 100 and self.pb.cash - qty * pr * exec_ratio - self.pb._commission(qty * pr * exec_ratio, is_sell=False) >= min_cash:
                    mv = qty * pr * exec_ratio
                    # 单笔最小名义额 (2026-09-04 方向3/摊薄固定成本):
                    # 取绝对小单门槛与相对净资产门槛较大者. 单笔太小则最低佣金+
                    # 滑点占比畸高, 抬到足够大才出手, 摊薄单位交易成本.
                    min_abs = PAPER.get("min_reorder_notional", 500.0) or 0.0
                    min_rel = equity_now * (float(PAPER.get("min_reorder_weight_pct", 0.0) or 0.0) / 100.0)
                    min_notional = max(min_abs, min_rel)
                    if mv < min_notional:   # 小单门槛: 低名义额补权不出手
                        log(f"跳过买入({canon}) 小单门槛(名义额{mv:.0f}<{min_notional:.0f})")
                        continue
                    if self._to_used + mv > to_budget:   # 换手预算门控(买入计入)
                        log(f"跳过买入({canon}) 超当日换手预算(已用{self._to_used:.0f}/{to_budget:.0f})")
                        continue
                    self.pb.buy(canon, qty, pr)
                    self._to_used += mv
                    log(f"买入({canon}) {qty}股 @{pr:.3f} 名义额{mv:.0f}")

        # 推进策略调仓日: 本窗口确有策略成交(卖出或买入)才锁定调仓间隔.
        if gate_open and self._to_used > 0:
            self._last_rebal_day = date.today().isoformat()
            log(f"策略调仓日推进 -> {self._last_rebal_day} (下次窗口≥{rebal_iv}自然日后)")

    # ---------- 午间重选 (独立线程, 不阻塞盘中 tick) ----------
    def _midday_reselect(self):
        """在午间(11:28配置时刻, 随tick检测触发)用盘中实时价重排目标池.
        拉全A实时快照 -> 用实时价替换最后一根日K的close重算因子 -> 重选TopN.
        完成后更新 self.targets, 下午 _rebalance 会自动: 卖出跌出池 / 补入新入选.
        因为拉全A实时价较慢(新浪源约30s), 需独立线程执行, 避免卡住实时撮合."""
        got = self._resel_lock.acquire(timeout=1)
        if not got:
            return
        try:
            log("午间重选: 开始拉全A实时快照...")
            spot = self.feed.fetch_all_spot()          # {canon: price}
            if not spot:
                log("午间重选: 实时快照为空, 跳过(沿用盘前目标池)")
                return
            db = StockDB()
            try:
                sel = RotationSelector(db, n=MAX_STOCKS).select(
                    real_time_prices=spot,
                    as_of=datetime.now().strftime("%Y-%m-%d %H:%M"),
                )
            finally:
                db.close()
            if "top_n" not in sel:
                log("午间重选: 选股失败, 沿用盘前目标池")
                return
            with self._resel_lock:
                self.targets = sel["top_n"]
                self.midday_targets = sel["top_n"]
                self.midday_result = {
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "scored": sel.get("scored"),
                    "basket_signal": sel.get("basket_signal"),
                    "top": [t["canon"] for t in sel["top_n"]],
                }
            # 落盘午间快照, 便于审计
            try:
                d = os.path.join(DAILY_DIR, self.cur_day.replace("-", ""))
                os.makedirs(d, exist_ok=True)
                sheet = {
                    "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "type": "midday_reselect",
                    "basket_signal": sel.get("basket_signal"),
                    "top_n": sel["top_n"],
                }
                _atomic_write_json(os.path.join(d, "midday_selection.json"), sheet)
            except Exception as e:
                log(f"午间重选落盘失败: {e}")
            log(f"午间重选完成: {len(sel['top_n'])}只 | basket_signal={sel.get('basket_signal')}")
        except Exception as e:
            log(f"午间重选异常: {type(e).__name__} {e}")
        finally:
            with self._resel_lock:
                self.midday_done = True

    # ---------- 持久化 ----------
    def _write_state(self, latest: dict, session: bool, live_src: str):
        snap = self.pb.snapshot()
        # 盘中实时权益
        positions = []
        target_set = {t["canon"] for t in self.targets}
        name_map = {t["canon"]: t.get("name", t["canon"]) for t in self.targets}
        for canon, p in snap["positions"].items():
            price = self.pb.market_price(canon)
            # A股规则实时字段
            lu = ld = None
            q = self.feed.quotes.get(canon) if hasattr(self.feed, "quotes") else None
            if q:
                lu, ld = q.get("limit_up"), q.get("limit_down")
            locked = p.get("locked_qty", 0)
            if p.get("buy_date") != self.pb.trade_date:
                locked = 0
            to_up = to_down = None
            if lu and price > 0:
                to_up = round((lu / price - 1) * 100, 2)
            if ld and price > 0:
                to_down = round((ld / price - 1) * 100, 2)
            trade_state = "可交易"
            if price <= 0:
                trade_state = "停牌/无价"
            elif lu and ld and price >= lu:
                trade_state = "涨停(不可买)"
            elif lu and ld and price <= ld:
                trade_state = "跌停(不可卖)"
            if p.get("buy_date") == self.pb.trade_date and locked > 0:
                trade_state += " · T+1锁定"
            positions.append({
                "canon": canon,
                "name": name_map.get(canon, canon),
                "qty": p["qty"],
                "avg_cost": round(p["avg_cost"], 3),
                "last_price": round(price, 3),
                "mv": round(p["qty"] * price, 2),
                "pnl_amt": round((price - p["avg_cost"]) * p["qty"], 2),
                "pnl_pct": round((price / p["avg_cost"] - 1) * 100, 2) if p["avg_cost"] else 0,
                "weight": round(p["qty"] * price / max(snap["equity"], 1), 4),
                "tplus1_locked": bool(locked > 0),
                "locked_qty": locked,
                "sellable_qty": max(p["qty"] - locked, 0),
                "limit_up": lu,
                "limit_down": ld,
                "to_limit_up_pct": to_up,
                "to_limit_down_pct": to_down,
                "trade_state": trade_state,
            })

        live = {
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "day": self.pb.trade_date,
            "mode": "盘中实时撮合" if session else "待机/收盘(仅价格刷新)",
            "in_session": session,
            "live_source": live_src,
            "feed_error": self.feed.last_error or "",
            "data_ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "capital": {
                "init_capital": INIT_CAPITAL,
                "equity": snap["equity"],
                "cash": snap["cash"],
                "market_value": snap["market_value"],
                "positions_value": snap["market_value"],
                "realized": snap["realized"],
                "unrealized": snap["unrealized"],
                "fees_paid": snap["fees_paid"],
                "buy_fees": snap["buy_fees"],
                "sell_fees": snap["sell_fees"],
                "attributed_pnl": snap["attributed_pnl"],
                "total_pnl": round(snap["equity"] - INIT_CAPITAL, 2),
                "total_pnl_pct": round((snap["equity"] / INIT_CAPITAL - 1) * 100, 2),
                "open_positions": snap["open_positions"],
                "cash_ratio": round(snap["cash"] / max(snap["equity"], 1), 4),
            },
            "positions": positions,
            "targets": [
                {"canon": t["canon"], "name": t.get("name", t["canon"]),
                 "score": t.get("score"), "signal": t.get("signal")}
                for t in self.targets
            ],
            "trades_today": self.pb.trades_today,
            "trades_history": self.pb.trades_history,
            "tick": self.tick,
            # 午间重选状态(页面提示用; midday_targets 为 None 表示未重选/沿用盘前)
            "midday": {
                "active": self.midday_targets is not None,
                "result": self.midday_result,
                "targets": (
                    [{"canon": t["canon"], "name": t.get("name", t["canon"]),
                      "score": t.get("score"), "signal": t.get("signal")}
                     for t in self.midday_targets]
                    if self.midday_targets else None
                ),
            },
            # A股规则配置(页面规则面板用)
            "rules": {
                "init_capital": INIT_CAPITAL,
                "max_stocks": MAX_STOCKS,
                "max_pos_ratio": MAX_POS_RATIO,
                "cash_cushion": round(1 - MAX_POS_RATIO, 4),
                "commission": PAPER["commission"],
                "stamp_tax": PAPER["stamp_tax"],
                "transfer_fee": PAPER["transfer_fee"],
                "slippage": PAPER["slippage"],
                "tplus1": PAPER["tplus1"],
                "lot_size": 100,
                "stop_loss_pct": round(PAPER.get("stop_loss", 0.03) * 100, 0),
                "max_single_weight_pct": round(PAPER.get("max_single_weight", 0.08) * 100, 0),
                "portfolio_drawdown_pct": round(PAPER.get("portfolio_drawdown", 0.08) * 100, 0),
                "board_limits": {"主板/ST": "±10%", "创业板/科创板": "±20%", "北交所": "±30%"},
            },
        }
        _atomic_write_json(LIVE_STATE, live)

        # 回写累计状态
        state = {
            "day": self.pb.trade_date,
            "equity": snap["equity"],
            "cash": snap["cash"],
            "realized": snap["realized"],
            "unrealized": snap["unrealized"],
            "fees_paid": snap["fees_paid"],
            "buy_fees": snap["buy_fees"],
            "sell_fees": snap["sell_fees"],
            "attributed_pnl": snap["attributed_pnl"],
            "positions": snap["positions"],
            "top_targets": tgt(self.targets),
            # 历史成交: {date -> [trade, ...]}, 跨日累积, 引擎重启后从 state.json 恢复
            "trades_history": self.pb.trades_history,
            # P2: 已入账的分红/除权事件key (防重启重复记账)
            "applied_corp_actions": sorted(self.pb.applied_corp),
            # 风控状态: 熔断状态与峰值权益跨重启延续, 避免重启绕过熔断
            "peak_equity": self.pb.peak_equity,
            "risk_state": self.pb.state,
        }
        _atomic_write_json(STATE_FILE, state)

    def loop(self):
        log(f"盘中引擎启动 | 目标池 {len(self.targets)}只 | interval={self.interval}s | 收盘15:05自动停止")
        try:
            while True:
                now = datetime.now()
                # 非交易日(周末+节假日) 或 收盘(15:03)后 自动停止,
                # 交由15:05收盘/维护任务独占写盘. 防节假日误撮合.
                try:
                    from trading_calendar import is_trading_day as _tc_day
                    _is_td = _tc_day(now)
                except Exception:
                    _is_td = now.weekday() < 5
                if (not _is_td) or (now.hour > 15 or (now.hour == 15 and now.minute >= 3)):
                    log("已收盘或非交易日, 引擎自动停止(次日由守护进程调度重启)")
                    break
                try:
                    self.run_tick()
                except Exception:
                    traceback.print_exc()
                time.sleep(self.interval)
        except KeyboardInterrupt:
            log("引擎停止(手动)")
        finally:
            self.close_ref_db()


def tgt(targets):
    return [t["canon"] for t in targets]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="单次运行")
    ap.add_argument("--interval", type=float, default=15.0, help="tick间隔(秒)")
    ap.add_argument("--allow-offsheet", action="store_true",
                    help="非交易时段也尝试刷新实时价(默认每tick拉, 失败即静默)")
    args = ap.parse_args()

    eng = RealtimeEngine(interval=args.interval, intraday_only=not args.allow_offsheet)
    if args.once:
        eng.run_tick()
        log("once 完成, live_state 已更新: " + LIVE_STATE)
    else:
        eng.loop()


if __name__ == "__main__":
    main()