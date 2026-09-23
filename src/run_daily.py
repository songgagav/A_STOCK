# ============================================================
# run_daily.py -- 全A轮动日频调度管道
# 方案A: 日频选股(DuckDB全A) + 盘中纸面撮合(AKShare)
# 每个交易日: 跑选股 -> 生成目标持仓 -> 纸面撮合 -> 每日汇总JSON
# ============================================================

import os
import json
import sys
from datetime import date as dt_date, datetime, timedelta

from config import DATA_DIR, DAILY_DIR, MAX_STOCKS, PAPER, STATE_FILE
from db import StockDB
from selector import RotationSelector, save_selection
from paper_book import PaperBook, PriceFeed


class _IngestSkipped(Exception):
    """内部信号: 数据源健康门禁判定 HALT, 本次跳过某一步摄入。

    为什么用异常而不是 if/else 层层缩进: 摄入步骤是一长串独立的 try/except,
    逐个加 if 会把缩进推深好几层且容易漏; 用异常能保证**一个地方判定,
    所有摄入步骤一致生效**。代价是必须显式捕获它, 否则会被下面的
    `except Exception` 记成"error"而掩盖真实原因(这正是本仓最忌讳的
    归因错误) —— 故每个摄入步骤都先 `except _IngestSkipped: pass`。
    """
    pass


def self_closed_loop(day: str, day_dir: str, db) -> dict:
    """D->B 反馈闭环: 当日信号 IC 回算(幂等写 ic_history.csv) + ICIR 驱动因子权重刷新.

    依赖两个 [D 反馈] 模块: ic_track(信号IC跟踪) 与 weight_optimizer(ICIR自适应权重).
    返回值写入 daily_summary.json 的 steps.feedback, 供 dashboard 查看.
    """
    fb = {"ic": {}, "weights": None, "note": "D反馈->B迭代: IC回算 + ICIR权重自适应"}
    try:
        import ic_track as icm
        from weight_optimizer import optimize_weights, WEIGHTS_FILE

        # a0) 滞后回填(核心修复): 当天跑当天时未来收益未发生, compute_day_ic 永远
        #     算不出 -> ic_history 从未被管道自动生成. 对已"到期"的历史天(其 signal日
        #     之后至少 max(hold) 根K线已入库)做增量补齐. append_ic 幂等, 能力内补齐.
        holds = [1, 3, 5, 10]
        try:
            today = db.latest_daily_bar_date()
            today_norm = icm._norm_date(today.date()) if today is not None else None
        except Exception:
            today_norm = None
        settled = icm.settle_mature_days(db, holds, today=today_norm)
        fb["backfilled_days"] = settled
        if settled:
            fb["note"] += f" | 滞后回填 {settled} 天(已到期IC)"

        # a) 当日信号 IC (信号日 = day_dir YYYYMMDD, 默认持有期 1/3/5/10)
        ic = icm.compute_day_ic(day_dir, db, holds)
        if ic:
            icm.append_ic(day_dir, ic)          # 幂等: 当日已存在则覆盖
            fb["ic"] = ic
            # ArcticDB factor_ic 持久化 (供 SPC/退化检测). ic 格式: {hold_h -> ic}
            try:
                from arctic_store import get_store
                store = get_store()
                ic_written = 0
                for hold_h, ic_val in ic.items():
                    factor_name = f"hold_{hold_h}"
                    if store.append_factor_ic(
                        factor_name, day,
                        ic=float(ic_val),
                        n=int(holds.index(hold_h) + 1) * 5,  # 估算样本量
                        recent_mean20=None,  # 无 rolling 历史, 后续可补
                    ):
                        ic_written += 1
                fb["arcticdb_ic_written"] = ic_written
            except Exception as e:
                fb["arcticdb_ic_error"] = str(e)[:120]
        else:
            fb["note"] += " | 当日样本不足(<5只配对未来收益), 未写IC"

        # b) ICIR 驱动权重再平衡 -> 写 data/weights.json (selector 运行时读取)
        opt = optimize_weights(window=40, alpha_share=0.28)
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(WEIGHTS_FILE, "w", encoding="utf-8") as f:
            json.dump(opt, f, ensure_ascii=False, indent=2)
        fb["weights"] = opt["weights"]
        fb["icir"] = {k: (v.get("icir") if v.get("icir") == v.get("icir") else None)
                      for k, v in opt["meta"]["icir"].items()}
        fb["generated"] = opt["meta"]["generated"]
    except Exception as e:
        fb["error"] = str(e)
        fb["note"] += " | 反馈步骤异常, 已跳过(不阻断收盘管道)"
    return fb


def update_market_sentiment(day: str) -> dict:
    """更新收盘市场情绪快照；指定日尚无数据时回退到数据库最近交易日。"""
    result = {"requested_day": day, "ok": False}
    try:
        from config import DUCKDB_PATH  # noqa: F401   (m4: 已退役, 保留引用以兼容)
        from market_panel import MARKET_DIR, compute_market

        market = compute_market(day)
        if not market.get("ok"):
            # [m4] 回退到最近可用交易日: 优先 StockDB(h5i 主源), 不再直连 DuckDB
            try:
                from db import StockDB
                latest = StockDB().latest_daily_bar_date()
                effective_day = str(latest)[:10] if latest is not None else None
            except Exception:
                effective_day = None
            if not effective_day:
                return {"ok": False,
                        "error": market.get("error", "市场情绪计算失败") + " 且无可用交易日"}
            if effective_day != str(day)[:10]:
                market = compute_market(effective_day)

        if not market.get("ok"):
            raise RuntimeError(market.get("error", "市场情绪计算失败"))

        out_dir = os.path.join(MARKET_DIR, market["day"].replace("-", ""))
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "market.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(market, f, ensure_ascii=False, indent=2)
        result.update({
            "ok": True,
            "effective_day": market["day"],
            "sentiment_score": market.get("sentiment_score"),
            "output": out_path,
        })
    except Exception as e:
        result["error"] = str(e)
    return result


def update_continuous_replay(day_dir: str, days: int = 10) -> dict:
    """用最新目标池更新连续交易日回放；异常只记录，不阻断其他收盘步骤。"""
    result = {"ok": False, "days": days}
    try:
        from backtest_engine import BacktestRunner

        replay = BacktestRunner(days=days).run(tag=f"daily_{day_dir}")
        result.update({
            "ok": True,
            "start": replay.get("start"),
            "end": replay.get("end"),
            "trade_days": replay.get("trade_days"),
            "total_return": replay.get("total_return"),
            "max_drawdown_pct": replay.get("max_drawdown_pct"),
            "output": os.path.join(DATA_DIR, "backtest_latest.json"),
        })
    except (Exception, SystemExit) as e:
        result["error"] = str(e)
    return result


def merge_valuation_snapshot() -> dict:
    """收盘后把 valuation_snapshot 全市场快照(覆盖>=4500行)并入 valuation 当日行。

    走 valuation_backfill.merge_snapshot_to_valuation (h5i 直写, 幂等跳过已有 symbol);
    异常只记录，不阻断收盘管道。
    """
    result = {"ok": False}
    try:
        from valuation_backfill import merge_snapshot_to_valuation

        rep = merge_snapshot_to_valuation(snapshot_day_or_latest=True)
        result.update(rep)
    except Exception as e:
        result["error"] = str(e)[:200]
    return result


def update_performance_attribution() -> dict:
    """基于全部日回执更新绩效归因报告，供 dashboard 收盘后直接读取。"""
    result = {"ok": False}
    try:
        import performance_report as pr

        rows = pr.load_daily_equities()
        report = pr.compute_report(rows, bench=True)
        if not report.get("ok"):
            raise RuntimeError(report.get("error", "绩效归因计算失败"))
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(pr.PERF_FILE, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        metrics = report.get("metrics") or {}
        period = report.get("period") or {}
        result.update({
            "ok": True,
            "period": period,
            "total_return": metrics.get("total_return"),
            "max_drawdown": metrics.get("max_drawdown"),
            "sharpe_annual": metrics.get("sharpe_annual"),
            "holdings_attributed": len(report.get("attribution") or []),
            "output": pr.PERF_FILE,
        })
    except Exception as e:
        result["error"] = str(e)
    return result


def run_spc_check(perf_df=None) -> dict:
    """盘后 SPC 监控: 从 ArcticDB 读取 perf_report 指标, 对每条跑 spc_check
    (含 LSL/USL 触顶独立告警维度).

    Args:
        perf_df: 可选, 预加载的 perf DataFrame (供 run_daily 在绩效归因后传入, 避免重复读)

    Returns:
        {
          "ok": bool,
          "indicators": [{indicator, level, n, last, mean, violations, message, pinning}],
          "worst_level": "P0|P1|P2|P3|OK",
          "any_pinning": bool,
          "arcticdb_written": bool,
        }
    """
    result = {"ok": False, "indicators": [], "worst_level": "OK", "any_pinning": False}
    try:
        from spc import spc_batch, DEFAULT_CFGS
        import pandas as pd

        if perf_df is None or perf_df.empty:
            from arctic_store import get_store
            store = get_store()
            perf_df = store.read_perf_reports(days=60)
        if perf_df is None or perf_df.empty:
            result["reason"] = "perf_report 无数据"
            return result

        # 1) 标准 perf 指标 -> SPC (含 max_drawdown LSL 触顶)
        series_map = {}
        if "total_return" in perf_df.columns:
            series_map["daily_return"] = perf_df["total_return"].astype(float).tail(40).reset_index(drop=True)
        if "max_drawdown" in perf_df.columns:
            series_map["max_drawdown"] = perf_df["max_drawdown"].astype(float).tail(40).reset_index(drop=True)
        if "sharpe_annual" in perf_df.columns:
            series_map["sharpe_rolling"] = perf_df["sharpe_annual"].astype(float).tail(40).reset_index(drop=True)

        # 2) IC 序列 (取任一因子的 IC 作为代表, 默认 trend)
        try:
            from arctic_store import get_store
            store = get_store()
            for f in ("trend", "signal", "govern", "liquidity", "vol", "mom_rev"):
                d = store.read_factor_ic(f)
                if d is not None and not d.empty and "ic" in d.columns:
                    series_map["ic_hold_5"] = d["ic"].astype(float).tail(40).reset_index(drop=True)
                    break
        except Exception:
            pass

        if not series_map:
            result["reason"] = "无可用序列"
            return result

        # 3) 批量 SPC
        indicators = spc_batch(series_map, DEFAULT_CFGS)

        # 4) 汇总 worst_level 与 any_pinning
        level_rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "OK": 4}
        worst = "OK"
        any_pin = False
        for r in indicators:
            lv = r.get("level", "OK")
            if lv in level_rank and level_rank[lv] < level_rank[worst]:
                worst = lv
            pin = r.get("pinning") or {}
            if pin.get("lsl_pinned") or pin.get("usl_pinned"):
                any_pin = True

        result["ok"] = True
        result["indicators"] = indicators
        result["worst_level"] = worst
        result["any_pinning"] = any_pin

        # 5) 落盘 ArcticDB daily_summary (子 symbol spc_<day>, 与 degradation 同模式)
        try:
            from arctic_store import get_store
            store = get_store()
            lib = store._lib("daily_summary")
            day_key = datetime.now().strftime("%Y-%m-%d")
            row = {
                "worst_level": worst,
                "any_pinning": any_pin,
                "n_indicators": len(indicators),
                "p0_count": sum(1 for r in indicators if r.get("level") == "P0"),
                "p1_count": sum(1 for r in indicators if r.get("level") == "P1"),
                "details_json": json.dumps(
                    [{k: r.get(k) for k in ("indicator", "level", "last", "n", "violations", "pinning")}
                     for r in indicators],
                    ensure_ascii=False, default=str,
                ),
            }
            df = pd.DataFrame([row], index=pd.to_datetime([day_key]))
            df.index.name = "day"
            sym_key = f"spc_{day_key}"
            if lib is not None:
                try:
                    existing = lib.read(sym_key).data
                    df = pd.concat([existing[existing.index != df.index[0]], df])
                    df = df[~df.index.duplicated(keep="last")]
                except Exception:
                    pass
                lib.write(sym_key, df)
                result["arcticdb_written"] = True
        except Exception as e:
            result["arcticdb_error"] = str(e)[:120]
    except Exception as e:
        result["error"] = str(e)[:200]
    return result


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"init_capital": None, "equity": None, "last_day": None, "positions": {}}


def save_state(st: dict):
    """P9: 原子写入 state. 先写同目录临时文件再 os.replace, 避免进程中断写坏半截JSON."""
    os.makedirs(DATA_DIR, exist_ok=True)
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _push_state_meta() -> dict:
    """把「已提交但未推送」的提交数记进回执(**开发纪律, 只记不告警**)。

    见 `daily_summary.json` 的 `push_state` 字段。**任何异常都不得冒泡** ——
    它是开发纪律的检查, 与交易管道无关; 一个 git 调用卡住不该影响收盘选股。

    判据与 `_tools/push_state.py` **同源**; 此处不 import 它, 因为 `_tools` 不是包,
    且不想给生产导入面添加开发期目录。两处都只看 `rev-list --left-right --count`
    的同一个数字, 逻辑极短, 重复的风险低于引入开发期依赖的风险。
    """
    try:
        import re as _re
        import subprocess as _sp
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        r = _sp.run(["git", "rev-list", "--left-right", "--count",
                     "origin/main...main"],
                    cwd=repo, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=20)
        if r.returncode != 0:
            return {"error": "git 不可用或远端分支缺失",
                    "detail": ((r.stderr or r.stdout or "").strip()[:160])}
        m = _re.match(r"\s*(\d+)\s+(\d+)", r.stdout or "")
        if not m:
            return {"error": f"无法解析 rev-list 输出: {(r.stdout or '')[:80]!r}"}
        return {"ahead": int(m.group(2)), "behind": int(m.group(1)),
                "note": ("开发纪律(非运行时纪律): ahead>0 = 有已提交但未推送的工作。"
                         "**只记日志, 不告警** —— 大重构期间 ahead>0 属正常, "
                         "强提醒会变成噪声(TableStale24h 330 次 firing 的教训)。")}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


#: 「连续 N 天未推送」的阈值(用户建议的更强保障)。
#: 为什么是**连续**而不是单日: 大重构期间单日 ahead>0 完全正常, 只有**持续**未推
#: 才说明真的漏了 —— 这与 `datasource_gate` 的"同一原因连续失败才停手"同一思路。
PUSH_STALE_DAYS = 3


def _push_state_streak(day: str, ahead: int) -> dict:
    """维护"连续多少天有未推送提交"的计数(落 `data/push_state_streak.json`)。

    **只为在日志里升级措辞, 刻意不接告警** —— 理由同上(开发纪律 ≠ 运行时纪律)。
    """
    import json as _json
    fp = os.path.join(DATA_DIR, "push_state_streak.json")
    st = {"day": None, "streak": 0}
    try:
        if os.path.exists(fp):
            with open(fp, encoding="utf-8-sig") as f:
                st = _json.load(f) or st
    except Exception:  # noqa: BLE001
        st = {"day": None, "streak": 0}
    if st.get("day") == day:
        return {"streak": int(st.get("streak") or 0), "unchanged": True}
    streak = (int(st.get("streak") or 0) + 1) if ahead > 0 else 0
    try:
        tmp = fp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump({"day": day, "streak": streak, "ahead": int(ahead)}, f,
                       ensure_ascii=False, indent=2)
        os.replace(tmp, fp)
    except Exception:  # noqa: BLE001
        pass
    return {"streak": streak, "unchanged": False,
            "stale": streak >= PUSH_STALE_DAYS, "threshold": PUSH_STALE_DAYS}


def run_daily(day: str = None, download_prices: bool = True, mode: str = "full") -> dict:
    """日频收盘 / 维护 管道.

    mode:
      'full'  (默认) 完整交易日管道: 数据拉取 + 选股(盘前决策产物) + 盘中状态归档
                     + 模型训练 + 反馈闭环. 交易日 15:05 调用.
      'maint'        周末/节假日维护管道: 仅数据拉取 + 因子/ArcticDB 刷新
                     + 模型训练/权重反馈. 跳过一切依赖"当日为新交易日"的步骤:
                     选股、市场情绪、连续回放、盘中持仓归档、绩效/SPC/退化/LLM.
                     (非交易日的日K无增量, 这些步骤结果无意义且会污染当日回执)

    返回 report dict(daily_summary.json 结构) 或 maint 聚合 dict.
    """
    day = day or dt_date.today().strftime("%Y-%m-%d")
    day_dir = day.replace("-", "")
    os.makedirs(os.path.join(DAILY_DIR, day_dir), exist_ok=True)

    db = StockDB()
    report = {"day": day, "mode": mode, "steps": {}}
    try:
        # 0) 盘前健康检查 (08:00~09:00 调度时也兼容执行). 失败不阻塞, 但 WARN/FAIL 写日志.
        #    maint 模式也检查, 便于发现周末数据通道问题.
        try:
            from premarket_healthcheck import run_healthcheck
            report["steps"]["premarket_healthcheck"] = run_healthcheck()
        except Exception as e:
            report["steps"]["premarket_healthcheck"] = {"ok": False, "error": str(e)[:200]}

        # 0.5) [2026-09-22 数据源健康门禁] **在摄入之前**判定数据源是否可信。
        #  为什么放在这里: 2026-09-22 的故障链是"引擎没跑 → 引擎静默停摆 → 直到
        #  收盘才发现今天没有新数据", 而症状与"今天是节假日"无法区分。启动闸门
        #  管启动那一刻、daemon 巡检管端口在不在, **都不管"服务活着但数据没进来"**。
        #
        #  停手范围(刻意区分): **只跳过摄入与依赖数据的下游动作(选股/因子视图/
        #  回测/训练)**; **保留选股/持仓归档**(自选股/持仓归档照常) —— 与
        #  kill_switch『只停新开仓, 绝不停离场』同一条纪律, 否则当天连持仓账都
        #  不更新, 运维就失去了判断"账户现在什么样"的依据。
        #
        #  门禁自身异常 => allow=True(不阻断)并按本仓纪律响亮记录。
        ds_allow = True
        try:
            import datasource_gate as _DG
            # 引擎**当场**探一次。为什么不能只看 `_prev`:
            # `_prev` 是**上一轮**的产物, 它答的是"上次那会儿引擎好不好", 不是"现在"。
            # 而这里位于摄入**之前** —— 此刻当天的 engine_bars_sync 还没跑, 没有产物可看;
            # 若不探, 引擎"服务活着但数据没进来"这一整类失效**在本次判定里根本不存在**
            # (2026-09-22 实测: 只传 sync_step/db_update_step 时 items 里连
            #  `stockdb_engine` 都不出现 —— 检查项静默消失, 比检查失败更危险)。
            # 探针失败(含超时/解码问题)**不阻断**: probe_engine 一律返回 ok=False 带原因,
            # 逐次计入"连续失败", 连续 3 次才 HALT。
            _pr = _DG.probe_engine()
            _eng_probe = _pr.get("probe") if _pr.get("ok") else {
                "ok": False, "error": _pr.get("error") or "探针失败"}
            # 上一轮的 engine_bars_sync / db_update 产物(若存在)一并喂进去 ——
            # 这样"连续失败"能在**本次摄入之前**就生效, 而不是等本次失败之后。
            _prev = {}
            try:
                import glob as _glob
                _dirs = sorted(_glob.glob(os.path.join(DAILY_DIR, "[0-9]" * 8)))
                _dirs = [d for d in _dirs if os.path.basename(d) < day.replace("-", "")]
                if _dirs:
                    with open(os.path.join(_dirs[-1], "daily_summary.json"),
                              encoding="utf-8-sig") as _f:
                        _prev = (json.load(_f).get("steps") or {})
            except Exception:  # noqa: BLE001
                _prev = {}
            _ds = _DG.check_and_record(engine_probe=_eng_probe,
                                       sync_step=_prev.get("engine_bars_sync"),
                                       db_update_step=_prev.get("db_update"),
                                       # [2026-09-23 修] 诚实回答"哪些观测还没入账":
                                       # 只有引擎探针是**当场新探**的; 上面两个 step 取自
                                       # **上一轮**产物(basename < 当天), 早已被上一轮记进账本。
                                       # 不声明 => 同一次失败被数两遍 => 提前一天 HALT。
                                       unrecorded=_DG.UNRECORDED_AT_DAILY_START)
            report["steps"]["datasource_gate"] = _ds
            ds_allow = bool(_ds.get("allow", True))
            if not ds_allow:
                print(f"[run_daily] 数据源健康门禁 HALT: {'; '.join(_ds.get('reasons') or [])}")
                print("[run_daily] => 跳过摄入与选股; **持仓归档照常**(停手不停离场)")
        except Exception as _e:  # noqa: BLE001
            report["steps"]["datasource_gate"] = {"ok": False, "error": str(_e)[:200]}

        # 1) 收盘后第一步: 增量补录数据库全表头数据 (断点续传, 已存在自动跳过)
        #    门禁 HALT 时**整段跳过** —— 不产出任何可能污染台账的产物。
        #    (不用异常做控制流: 那会让下面的 except 把它记成"error", 掩盖真实原因。)
        def _skip_ingest(step: str) -> dict:
            return {"ok": False, "skipped": "datasource_gate_halt",
                    "note": (f"数据源健康门禁判定 HALT, 跳过 {step} 摄入 —— "
                             f"不产出可能污染台账的产物; 持仓归档照常")}

        try:
            if not ds_allow:
                report["steps"]["db_update"] = _skip_ingest("db_update")
                raise _IngestSkipped()
            from update_db import update_all
            upd = update_all(day=datetime.strptime(day, "%Y-%m-%d").date(),
                             only=["daily_bars", "valuation_snapshot",
                                   "adj_factors", "northbound_money",
                                   "margin_daily", "dzjy_daily",
                                   "money_flow_estimate", "lhb",
                                   "stock_news", "events",
                                   "orderbook_snapshot", "block_trade"])
            # 提取 AKShare 统计 (网络/空数据诊断)
            ak_stats = upd.pop("__akshare_stats__", {})
            tables_dict = {k: {"ok": v["ok"], "rows": v["rows"]} for k, v in upd.items()}
            # ok 判定: 至少有任一表成功 OR AKShare 网络错误已知容错
            any_ok = any(v.get("ok") for v in upd.values())
            report["steps"]["db_update"] = {
                "ok": any_ok,
                "tables": tables_dict,
                "akshare_stats": ak_stats,
                "note": (
                    "AKShare 在非交易时段 / 网络受限 / 接口变更时返回空数据; "
                    "daily_bars 路径已由 free_stockdb_sync 兜底"
                ),
            }
        except _IngestSkipped:
            pass                       # 已如实记入 report, 不再覆盖成 generic error
        except Exception as e:
            report["steps"]["db_update"] = {"ok": False, "error": str(e)[:200]}

        # 1.05) 行情增量摄入。**主源 = 厂商引擎 SDK 直连**（2026-09-21 换源, 用户决策 A）;
        #        离线镜像 kline_parts 自 09-04 冻结、生成器在本机失踪（登记册 P1-MIRRORDEAD）,
        #        故降级为**回退路径**。切换必须有留痕: 报告里写清用了哪个源、为什么回退 ——
        #        "两个源"若无记录, 就会变成无人察觉的口径漂移。
        _eng = None
        try:
            if not ds_allow:
                report["steps"]["engine_bars_sync"] = _skip_ingest("engine_bars_sync")
                raise _IngestSkipped()
            from engine_bars_sync import sync_to_latest
            _eng = sync_to_latest(apply=True)
            report["steps"]["engine_bars_sync"] = {
                "ok": _eng.get("ok"),
                "used": _eng.get("used"),
                "h5i_max_before": _eng.get("h5i_max_before"),
                "h5i_max_after": _eng.get("h5i_max_after"),
                "engine_last_day": _eng.get("engine_last_day"),
                "appended": _eng.get("appended"),
                "planned_days": _eng.get("planned_days"),
                "missing_trading_days": _eng.get("missing_trading_days"),
                "truncated_days": _eng.get("truncated_days"),
                "errors": _eng.get("errors"),
                "note": "厂商引擎 SDK 直连(stockdb.exe @127.0.0.1:7899) 主源",
            }
        except _IngestSkipped:
            pass
        except Exception as e:  # noqa: BLE001
            report["steps"]["engine_bars_sync"] = {"ok": False, "error": str(e)[:300]}

        if not ds_allow:
            # 门禁 HALT: 镜像回退路径同样跳过 —— 数据源不可信时,
            # "换个源再抓一遍"只会把不可信数据从另一条路灌进台账。
            report["steps"]["free_stockdb_sync"] = _skip_ingest("free_stockdb_sync")
        elif isinstance(_eng, dict) and _eng.get("ok"):
            # 主源成功: 不再跑镜像 —— 它已冻结, 跑它只会 scanned=0 白等约 1 分钟。
            report["steps"]["free_stockdb_sync"] = {
                "ok": True, "skipped": True,
                "note": "已由 engine_bars_sync(主源) 完成; 镜像路径跳过 —— kline_parts 自 "
                        "2026-09-04 冻结且生成器失踪(登记册 P1-MIRRORDEAD)",
            }
        else:
            # 回退镜像: 必须先记录**为什么**回退
            _why = "engine_bars_sync 未成功"
            if isinstance(_eng, dict):
                _why = (_eng.get("error")
                        or ", ".join(_eng.get("errors") or [])
                        or _why)
            else:
                _why = (report["steps"].get("engine_bars_sync") or {}).get("error") or _why
            try:
                from free_stockdb_sync import refresh_parquet_from_duckdb, sync_incremental
                # [2026-09-05 既有修复, 保留] since_date 强制近窗, 覆盖"水位线跳洞"问题。
                _recent = (datetime.now() - timedelta(days=12)).strftime("%Y-%m-%d")
                sync = sync_incremental(max_workers=8, since_date=_recent,
                                        progress_every=2000)
                parquet_refresh = refresh_parquet_from_duckdb()
                report["steps"]["free_stockdb_sync"] = {
                    "ok": sync.get("ok"),
                    "fallback": True,
                    "fallback_reason": _why,
                    "scanned": sync.get("scanned"),
                    "new_rows": sync.get("new_rows"),
                    "symbols_with_data": sync.get("symbols_with_data"),
                    "min_new_date": sync.get("min_new_date"),
                    "max_new_date": sync.get("max_new_date"),
                    "elapsed_seconds": sync.get("elapsed_seconds"),
                    "parquet_refresh": parquet_refresh,
                    "note": sync.get("note") or "回退源: 镜像 kline_parts (free_stockdb <-> daily_bars)",
                }
            except Exception as e:  # noqa: BLE001
                report["steps"]["free_stockdb_sync"] = {"ok": False, "fallback": True,
                                                        "error": str(e)[:200]}

        # 1.055) change_pct 历史回填: 增量同步后, 扫描 daily_bars 把 change_pct IS NULL
        #        且前一日 close 在 DuckDB 中可得的行, 用 (cur-prev)/prev*100 重算.
        #        这是回灌 / 老数据 / 跨源数据 first-row 的标准修复步骤.
        try:
            from backfill_change_pct import run_backfill
            bf = run_backfill()
            report["steps"]["backfill_change_pct"] = {
                "ok": bf.get("ok"),
                "filled_non_first": bf.get("filled_non_first"),
                "filled_first": bf.get("filled_first"),
                "filled_total": bf.get("total_filled"),
                "remaining_nulls": bf.get("remaining_nulls"),
                "total_rows": bf.get("total_rows"),
                "note": bf.get("note", "盘后 change_pct 回填, 用 DuckDB 前一日 close 重算"),
            }
        except Exception as e:
            report["steps"]["backfill_change_pct"] = {"ok": False, "error": str(e)[:200]}

        # 1.058) 收盘后快照并入 valuation: 把当日(或最近一次) valuation_snapshot
        #      全市场快照(覆盖>=4500行)并入 valuation 当日行(source='snapshot')。
        #      已并入的 symbol 跳过, 幂等; 异常不阻断收盘管道。
        try:
            report["steps"]["valuation_snapshot_merge"] = merge_valuation_snapshot()
        except Exception as e:
            report["steps"]["valuation_snapshot_merge"] = {"ok": False,
                                                           "error": str(e)[:200]}

        # 1.059) 融合因子每日打分样本归档: 收盘后以当日可见数据出分并落 parquet,
        #       供滚动 IC/ICIR/分组监控(factor_m5_close.status)使用; 幂等, 异常不阻断。
        try:
            from factor_m5_close import record_day
            report["steps"]["fusion_sample_record"] = record_day()
        except Exception as e:
            report["steps"]["fusion_sample_record"] = {"ok": False,
                                                       "error": str(e)[:200]}

        # 1.0591) gp4 观察候选每日截面样本归档 (data/factor_gp4_samples):
        #       GP 月频挖掘候选 (|rev_yoy_neu|-np_yoy_neu-roe_yy_chg_neu)/
        #       (|roe_neu|+sqrt(bvps_neu)), 日频中性化口径同 factor_fusion;
        #       供 gp4_daily_validate.status 滚动 IC 观察 (不参与融合权重);
        #       幂等, 异常不阻断。
        try:
            from gp4_daily_validate import record_day_gp4
            report["steps"]["gp4_sample_record"] = record_day_gp4()
        except Exception as e:
            report["steps"]["gp4_sample_record"] = {"ok": False,
                                                    "error": str(e)[:200]}

        # 1.0595) 两融(margin_daily)每日同步: 官方交易所市场级两融余额汇总
        #       (沪=上交所汇总, 深=深交所汇总, 京=北交所汇总; 单位统一元/股),
        #       h5i 整日单调追加 + 幂等(同日已存在跳过); 深/京接口 T+1 发布,
        #       由次日运行自愈补齐; 非阻断。
        try:
            from margin_sync import sync_margin
            report["steps"]["margin_sync"] = sync_margin(days=1, day=day)
        except Exception as e:
            report["steps"]["margin_sync"] = {"ok": False, "error": str(e)[:200]}

        # 1.0596) 北向/港股通资金流(northbound_money)每日续更: 只把 max(ts) 之后
        #       新到交易日整日 4 行以同结构追加到 h5i (幂等); 非阻断。
        try:
            from northbound_sync import sync_northbound
            report["steps"]["northbound_sync"] = sync_northbound(day=day)
        except Exception as e:
            report["steps"]["northbound_sync"] = {"ok": False, "error": str(e)[:200]}

        # 1.5) 因子物化视图刷新. DuckDB 在 read_only 下不能 CREATE, 必须用写连接.
        #      dashboard 端的 _duckdb_query 是 read_only, 通过全局锁串行化, 不会与本次写冲突.
        try:
            from build_factor_views import build_views
            report["steps"]["factor_views"] = build_views(read_only_db=False)
        except Exception as e:
            report["steps"]["factor_views"] = {"ok": False, "error": str(e)[:200]}

        # 1.6) vnpy/ArcticDB 增量入库: 把当日 (及近端新增的) daily_bars 数据
        #      灌入 ArcticDB bars lib, 为后续 vnpy 回测/研究准备数据底座.
        #      全量首次跑约 12 分钟; --incremental 仅补最新一日, 通常 <10s.
        try:
            import pandas as pd
            from vnpy_full_pull import _read_full_daily_bars
            from arctic_store import get_store
            store = get_store()
            existing = set(store.list_bars_symbols())
            bars_map = _read_full_daily_bars()
            written = updated = skipped = failed = 0
            for sym, df in bars_map.items():
                if df is None or df.empty:
                    skipped += 1
                    continue
                df = df.copy()
                if "date" in df.columns:
                    df["date"] = pd.to_datetime(df["date"])
                    df = df.set_index("date")
                df = df[~df.index.duplicated(keep="last")].sort_index()
                # 增量: 仅补 DuckDB 晚于 ArcticDB 现有最大日期的部分
                if sym in existing:
                    try:
                        last_ts = store.read_bars(sym).index.max()
                        df = df[df.index > last_ts]
                        if df.empty:
                            skipped += 1; continue
                    except Exception:
                        pass
                try:
                    ok = store.write_bars(sym, df.reset_index())
                    if ok:
                        if sym in existing:
                            updated += 1
                        else:
                            written += 1
                except Exception:
                    failed += 1
            report["steps"]["arcticdb_pull"] = {
                "ok": failed == 0,
                "new": written,
                "updated": updated,
                "skipped": skipped,
                "failed": failed,
                "total_arcticdb": len(store.list_bars_symbols()),
                "note": "盘后增量入库; 全量首次跑需 ~12min, --incremental 模式 <10s",
            }
        except Exception as e:
            report["steps"]["arcticdb_pull"] = {"ok": False, "error": str(e)[:200]}

        # 1) 选股 (交易日: 生成次日目标池 = 盘前决策产物).
        #    非交易日(maint): 跳过选股/市场情绪/连续回放, 因为无新交易数据.
        sel = None
        if mode == "full" and not ds_allow:
            # [数据源健康门禁] HALT 时不选股 —— 用不可信数据选出的池会污染台账,
            # 且次日消费方无从区分"这是正常选出的池"还是"这是带病选出的池"。
            # **持仓归档照常**(见下方 paper/book 段) —— 停手不停离场。
            report["steps"]["select"] = {
                "skip": True, "skipped": "datasource_gate_halt",
                "reason": "数据源健康门禁判定 HALT, 跳过选股(避免带病产物污染台账)"}
            report["steps"]["market_sentiment"] = {"ok": False, "skip": True,
                                                   "reason": "数据源门禁 HALT 跳过"}
            report["steps"]["continuous_replay"] = {"ok": False, "skip": True,
                                                    "reason": "数据源门禁 HALT 跳过"}
        elif mode == "full":
            selector = RotationSelector(db, n=MAX_STOCKS)
            sel = selector.select()
            sel["date"] = day
            save_selection(sel, day_dir)
            report["steps"]["select"] = {
                "universe": sel["universe_total"],
                "filtered": sel["filtered"],
                "scored": sel["scored"],
                "top": [t["canon"] for t in sel.get("top_n", [])],
            }

            # 2) 更新市场情绪与连续交易日回放。两项均独立容错，不阻断持仓归档。
            report["steps"]["market_sentiment"] = update_market_sentiment(day)
            report["steps"]["continuous_replay"] = update_continuous_replay(day_dir, days=10)
        else:
            report["steps"]["select"] = {"skip": True,
                                         "reason": "非交易日(maint), 跳过选股/市场情绪/连续回放"}
            report["steps"]["market_sentiment"] = {"ok": False, "skip": True,
                                                   "reason": "非交易日(maint) 跳过"}
            report["steps"]["continuous_replay"] = {"ok": False, "skip": True,
                                                    "reason": "非交易日(maint) 跳过"}

        # 1.7) f_ml 融合因子样本积累 + 到期收益回填(独立容错).
        #      full: 用当日完整候选池(pool_snapshot)重算权威 f_ml 落样本表;
        #      full+maint: 对已到期的历史样本回填真实未来5日收益(供换模/DRL注入).
        try:
            from fml_accumulate import accumulate_day, settle_mature, summary
            if mode == "full":
                fmla = accumulate_day(day, day_dir)
            else:
                fmla = {"skip": True, "reason": "非交易日(maint) 无当日选股池, 跳过当日样本"}
            fmls = settle_mature(db, hold=5)   # 两种模式都回填到期样本
            report["steps"]["fml_accumulate"] = {
                "ok": fmla.get("ok", False), "day": day,
                "accumulate": fmla,
                "settle": fmls,
                "table": summary(),
            }
        except Exception as e:
            report["steps"]["fml_accumulate"] = {"ok": False, "error": str(e)[:200]}

        # 2.5) vnpy 验证回测 + DRL 微调; 均独立容错
        try:
            from vnpy_backtest import run_vnpy_backtest
            report["steps"]["vnpy_backtest"] = run_vnpy_backtest(day, top_n=10, lookback_days=120)
        except Exception as e:
            report["steps"]["vnpy_backtest"] = {"ok": False, "error": str(e)[:200]}

        # 2.6) [路线图 #9/#11 接线] 多场景回测 + 多策略组合回测 (2026-09-22)
        #  为什么接在日更里: 这两件事的价值都是**分布性质** ——
        #    · 多场景: 单日只能说明"这天如何", 成本敏感性要在多天上平均;
        #    · 组合回测: "组合相对单腿改善了多少"必须与单腿**同一撮合口径**才可比。
        #  故每天跑一段滚动窗口并把结果落盘, 累积成面板。
        #  性能: 窗口由 PAPER.vnpy_regime_days / portfolio_bt_days 控制(默认 5/10),
        #  两者都**独立容错** —— 回测失败不得影响选股/持仓归档主链路。
        #  注意: 组合回测走 hist_pit(候选目录 C < D 的盘前视角), 与虚拟盘同源但
        #  **不调用** load_targets(它依赖调用时刻的磁盘状态, 事后跑会停在不同档)。
        try:
            from config import PAPER as _P
            _pdays = int(_P.get("portfolio_bt_days", 10) or 0)
            if _pdays > 0:
                import portfolio_live as _PL
                _win = _PL.recent_trading_days(_pdays)
                report["steps"]["portfolio_backtest"] = _PL.run_live_portfolio_backtest(
                    _win, live_pool=False,
                    save_to=os.path.join(DATA_DIR, "portfolio_backtest_latest.json"))
            else:
                report["steps"]["portfolio_backtest"] = {
                    "ok": False, "skip": True,
                    "reason": "PAPER.portfolio_bt_days=0 已关闭"}
        except Exception as e:
            report["steps"]["portfolio_backtest"] = {"ok": False, "error": str(e)[:200]}

        try:
            from config import PAPER as _P2
            _rdays = int(_P2.get("vnpy_regime_days", 5) or 0)
            _scen = list(_P2.get("regime_scenarios") or ["normal", "stress"])
            if _rdays > 0:
                from vnpy_backtest import run_regime_batch
                from h5i_bar_store import H5iBarStore
                _all = list(H5iBarStore().trading_days())
                # 只取"未来数据充足"的日子(forward 窗口需要 lookback 根之后的数据),
                # 否则末尾几天必然报"未来数据不足" —— 那是日期选取问题, 不是策略问题。
                _lb = 20
                _cand = _all[-(_rdays + _lb):- 1] if len(_all) > _rdays + _lb else _all[:-1]
                _pick = _cand[-_rdays:] if _cand else []
                if _pick:
                    _rb = run_regime_batch(_pick, _scen, top_n=10, lookback_days=_lb,
                                           forward=True, persist_arctic=False)
                    _rb["days_used"] = _pick
                    report["steps"]["regime_scenarios"] = _rb
                else:
                    report["steps"]["regime_scenarios"] = {
                        "ok": False, "skip": True, "reason": "可用交易日不足"}
            else:
                report["steps"]["regime_scenarios"] = {
                    "ok": False, "skip": True, "reason": "PAPER.vnpy_regime_days=0 已关闭"}
        except Exception as e:
            report["steps"]["regime_scenarios"] = {"ok": False, "error": str(e)[:200]}

        # 2.7) [路线图 ⑭ 接线] 因子假设的日更巡检: lint -> 假设级证据 -> (通过者)收益评估。
        #  为什么值得每天跑: 这是唯一一条**先问"逻辑成不成立"再看收益**的路径,
        #  它每天都在消耗当日的因子截面 —— 而截面一旦过去就补不回来。
        #  假设来源: data/factor_hypotheses_proposed.json(没有就 skip, 不凭空造假设)。
        try:
            from config import PAPER as _P3
            _hd = int(_P3.get("hypothesis_days", 60) or 0)
            _hpath = os.path.join(DATA_DIR, "factor_hypotheses_proposed.json")
            if _hd > 0 and os.path.exists(_hpath):
                import json as _json
                import factor_hypothesis as _FH
                import factor_hypothesis_eval as _FHE
                with open(_hpath, encoding="utf-8") as _f:
                    _raw = _json.load(_f)
                _hyps = [(_FH.Hypothesis(**h) if isinstance(h, dict) else h)
                         for h in (_raw.get("hypotheses") if isinstance(_raw, dict) else _raw)]
                from h5i_bar_store import H5iBarStore as _HBS
                _alld = list(_HBS().trading_days())
                _evd = _alld[-_hd:] if len(_alld) > _hd else _alld
                report["steps"]["factor_hypotheses"] = _FHE.run_hypothesis_evaluation(
                    _evd, _hyps, evaluate=True)
            else:
                report["steps"]["factor_hypotheses"] = {
                    "ok": False, "skip": True,
                    "reason": ("PAPER.hypothesis_days=0 已关闭" if _hd <= 0
                               else f"无假设文件 {_hpath}")}
        except Exception as e:
            report["steps"]["factor_hypotheses"] = {"ok": False, "error": str(e)[:200]}

        # 2.8) [P2-DEGRADE-INPUTS] 降级驱动量**采集**(拒单率/滑点分布/参与率)。
        #  只采集不判定: 进 health_state 需要一个"拒单率多高算降级"的阈值, 而本仓
        #  纪律是「阈值须基于下游表现」。故每日把分布算出来落盘, 累积后再标定。
        #  **不参与任何拦截** —— 这一点由 degrade_inputs.report() 的
        #  wired_into_state_machine=False 显式声明, 并有测试锁住。
        try:
            import degrade_inputs as _DI
            report["steps"]["degrade_inputs"] = _DI.report()
        except Exception as e:
            report["steps"]["degrade_inputs"] = {"ok": False, "error": str(e)[:200]}

        try:
            from pre_drl_brief import run_pre_drl_brief
            report["steps"]["pre_drl_brief"] = run_pre_drl_brief(day, day_dir)
        except Exception as e:
            report["steps"]["pre_drl_brief"] = {"ok": False, "error": str(e)[:200]}

        # ===== DRL 运行环境门禁（用户决策 D, 2026-09-20）=====
        # 为什么门禁必须在 `import drl_train` **之前**: drl_train 顶层 `import torch`，
        # 缺依赖的解释器会在导入处抛 ImportError，被下面的 except 吞成 {"ok": False} ——
        # 那就是"没有账本、没有告警、没有 L3"的静默路径。drl_degrade 是**轻量**模块
        # （无 torch / 无 h5i_db），故能在此安全探测并**显式**置 L3。
        # 用户决策 D: DRL 暂不在生产运行, 且**不得**回退旧模型继续生成信号
        # （环境已损坏, 用陈旧模型下单的风险高于停一天）。
        _drl_probe = None
        _drl_forced = None
        try:
            import drl_degrade
            _drl_probe = drl_degrade.probe_runtime()
            if not _drl_probe.get("ok"):
                _why = (f"DRL 运行环境不可用: 缺 {'/'.join(_drl_probe['missing'])} "
                        f"(python={_drl_probe['python']}); "
                        f"决策 D: 显式置 L3 且不回退旧模型")
                _drl_forced = drl_degrade.force_halt(day, _why, extra={"probe": _drl_probe})
                report["steps"]["drl_train"] = {
                    "ok": False, "skipped": "drl_env_unavailable",
                    "error": _why, "degrade": _drl_forced, "probe": _drl_probe,
                }
                print(f"[run_daily] DRL L3（显式暂停）: {_why}")
        except Exception as e:
            report["steps"]["drl_probe"] = {"ok": False, "error": str(e)[:200]}

        if _drl_forced is None:
            try:
                from drl_train import run_drl_train
                from config import CVAR_PPO as _CVAR_CFG
                report["steps"]["drl_train"] = run_drl_train(
                    day, total_timesteps=800,
                    cvar_alpha=_CVAR_CFG["cvar_alpha"],
                    cvar_coef=_CVAR_CFG["cvar_coef"],
                )
            except Exception as e:
                report["steps"]["drl_train"] = {"ok": False, "error": str(e)[:200]}

        # ===== DRL 降级状态进每日复盘（可见性: 用户要求"每日复盘告警可见"）=====
        # 无论走哪条路径都写: 复盘要能一眼看到 DRL 当前级别与最近一次降级事件。
        try:
            import drl_degrade as _dd
            _lv = _dd.current_level()
            report["steps"]["drl_degrade"] = {
                "ok": True, "level": _lv["level"], "level_name": _lv["level_name"],
                "severity": _lv["severity"], "blocked_plan": _lv["blocked_plan"],
                "source_day": _lv["source_day"],
                "ledger": _dd.event_ledger_path(),
                "event_count": _dd.event_count(),
                "last_event": _dd.last_event(),
                "probe": _drl_probe,
            }
            if _lv["blocked_plan"]:
                # 复盘状态必须反映"当日 DRL 未产出新信号", 不能仍报 OK。
                report["drl_status"] = (f"{_lv['severity']}: DRL {_lv['level_name']}"
                                        f"（未生成新信号）")
        except Exception as e:
            report["steps"]["drl_degrade"] = {"ok": False, "error": str(e)[:200]}

        # ===== DRL 学习后检查（DRL-3, 只记录数值）=====
        # 放在这里而不是 run_drl_train 内部: drl_post 是**轻量**模块（无 torch/h5i_db）,
        # 故**决策 D 之下（DRL 不训练）依然每天记录**滚动窗口与决策一致性 ——
        # 否则一旦 DRL 停摆, 学习后检查会跟着一起"消失", 复盘就断了。
        try:
            import drl_post
            _post = drl_post.record_post_metrics(day)
            report["steps"]["drl_post"] = {
                "ok": True, "ledger": drl_post.ledger_path(),
                "rolling": _post.get("rolling_window"),
                "topn_jaccard": _post.get("topn_jaccard"),
                "unavailable": list((_post.get("unavailable") or {}).keys()),
                "threshold_applied": _post.get("threshold_applied"),
            }
        except Exception as e:
            report["steps"]["drl_post"] = {"ok": False, "error": str(e)[:200]}

        # ‌) 存档当前模拟盘状态 (不调 rebalance! 盘中引擎的实时持仓原样保留,
        #    收盘任务只负责"生成次日总标池"与"当日状态归档", 不覆盖盘中调仓结果)
        #    非交易日(maint): 无当日盘中状态, 跳过归档与汇总.
        if mode == "full":
            pb = PaperBook()
            pb.restore(load_state())   # 只读恢复盘中引擎的实时现金/持仓
            target = [
                {
                    "canon": t["canon"],
                    "price": t["price"],
                }
                for t in sel.get("top_n", [])
            ]
            snap = pb.snapshot()
            pb.save_daily(day_dir)
            report["steps"]["paper"] = snap
            # 4)立即汇总 (不写全局 state.json! 那是盘中引擎的领地.
            report["summary"] = {
                "equity": snap["equity"],
                "cash": snap["cash"],
                "open_positions": snap["open_positions"],
                "trades": len(pb.trades_today),
            }
        else:
            snap = None
            report["steps"]["paper"] = {"skip": True,
                                       "reason": "非交易日(maint), 无当日盘中状态"}
            report["summary"] = {"skip": True,
                                 "reason": "非交易日(maint) 跳过"}

        # 先写当日回执，绩效归因会从 daily_summary.json 读取包括今天在内的权益。
        out_path = os.path.join(DAILY_DIR, day_dir, "daily_summary.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        # [2026-09-22 DISC-2 实例: 开发纪律[已提交 vs 已推送]] 记入回执, **只记日志**。
        #
        # 为什么在这里: 本会话踩到过"commit 了 35 次却一次没推" —— 而 `git status`
        # 只显示未提交文件, **不告诉你 ahead N**, 于是"看起来一切已保存"。
        #
        # **刻意不做强提醒/不推告警**: 它是**开发纪律**(提交/推送规范), 不是运行时纪律
        # (数据源健康/告警链)。大重构期间 ahead>0 完全正常, 强提醒会变成噪声 ——
        # 那正是 `TableStale24h` 330 次 firing 的教训(长期无人处理的告警等于没有告警)。
        # 故: 写进回执可随时查, 但不打扰。
        #
        # 放在**回执落盘之后**: git 子进程即便卡住, 也不会拖慢回执本身。
        try:
            _ps = _push_state_meta()
            # 「连续 N 天」只在拿到 ahead 时才算 —— 取不到状态时不得推进计数
            # (否则一个 git 故障会被记成"连续未推")。
            if "ahead" in _ps:
                _ps.update(_push_state_streak(day_dir, _ps["ahead"]))
            report["push_state"] = _ps
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2, default=str)
            if _ps.get("ahead"):
                _msg = f"{_ps['ahead']} 个提交未推送 (仅记录, 不告警)"
                if _ps.get("stale"):
                    _msg += f" —— **已连续 {_ps['streak']} 天**, 建议尽快推"
                print(f"[run_daily] [push_state] {_msg}")
        except Exception as e:  # noqa: BLE001
            # 开发纪律的检查**绝不能**影响运行时管道
            report["push_state"] = {"error": f"{type(e).__name__}: {e}"}
        # ===== ArcticDB daily_summary 持久化 (供 SPC/退化检测查询) =====
        try:
            from arctic_store import get_store
            if get_store().write_daily_summary(day, report):
                report["steps"]["arcticdb_daily_summary"] = "ok"
        except Exception as e:
            report["steps"]["arcticdb_daily_summary"] = f"err: {str(e)[:120]}"

        # 5) 绩效归因 + SPC + 退化 + LLM 点评.
        #    仅交易日: 依赖当日新交易数据产生的 daily_summary/持仓, 非交易日跳过.
        if mode == "full":
            # 5) 绩效归因：净值、基准超额、回撤、持仓贡献、IC 与回放基线。
            report["steps"]["performance_attribution"] = update_performance_attribution()

            # 5.6) 盘后 SPC 监控 (含 LSL 触顶独立告警维度).
            #      读 ArcticDB perf_report -> spc_batch -> worst_level + any_pinning.
            #      异常容错, 不阻断主流程.
            try:
                spc_out = run_spc_check()
                report["steps"]["spc_check"] = {
                    "ok": spc_out.get("ok"),
                    "worst_level": spc_out.get("worst_level"),
                    "any_pinning": spc_out.get("any_pinning"),
                    "n_indicators": len(spc_out.get("indicators") or []),
                    "p0_count": sum(1 for r in (spc_out.get("indicators") or []) if r.get("level") == "P0"),
                    "p1_count": sum(1 for r in (spc_out.get("indicators") or []) if r.get("level") == "P1"),
                    "indicators_summary": [
                        {
                            "indicator": r.get("indicator"),
                            "level": r.get("level"),
                            "last": r.get("last"),
                            "violations": (r.get("violations") or [])[:3],
                            "pinning": r.get("pinning"),
                        }
                        for r in (spc_out.get("indicators") or [])
                    ],
                    "arcticdb_written": spc_out.get("arcticdb_written"),
                    "arcticdb_error": spc_out.get("arcticdb_error"),
                    "reason": spc_out.get("reason"),
                    "error": spc_out.get("error"),
                }
            except Exception as e:
                report["steps"]["spc_check"] = {"ok": False, "error": str(e)[:200]}

            # 5.7) 策略退化检测: 退化指数 + 增量样本 (供 6.5 增量学习复用)
            try:
                from degradation import run_full_check
                deg = run_full_check(days=30)
                di = deg.get("degradation_index", {}) or {}
                report["steps"]["degradation"] = {
                    "ok": True,
                    "overall_score": di.get("overall_score"),
                    "worst_level": di.get("worst_level"),
                    "components": di.get("components", []),
                    "samples_n": len(deg.get("incremental_samples", [])),
                }
            except Exception as e:
                report["steps"]["degradation"] = {"ok": False, "error": str(e)[:200]}

            # 5.5) LLM 盘后归因点评 (MiniMax, Anthropic 兼容协议). 独立容错,
            #      失败不阻断 D 反馈闭环. 读 performance_report.json + market.json.
            try:
                from llm_commentary import run_llm_commentary
                report["steps"]["llm_commentary"] = run_llm_commentary(day, day_dir)
            except Exception as e:
                report["steps"]["llm_commentary"] = {"ok": False, "error": str(e)[:200]}
        else:
            report["steps"]["performance_attribution"] = {"skip": True,
                                                          "reason": "非交易日(maint) 跳过"}
            report["steps"]["spc_check"] = {"ok": False, "skip": True,
                                            "reason": "非交易日(maint) 跳过"}
            report["steps"]["degradation"] = {"ok": False, "skip": True,
                                              "reason": "非交易日(maint) 跳过"}
            report["steps"]["llm_commentary"] = {"skip": True, "reason": "非交易日(maint) 跳过"}

        # 5.8) Agent 编排层 (P1-P3): AlphaLogics 因子发现 + FactorMAD 辩论 + 综合研判
        if mode == "full":
            try:
                from agent_orchestrator import post_market_analysis
                report["steps"]["agent_orchestrator"] = post_market_analysis(day, use_llm=True)
            except Exception as e:
                report["steps"]["agent_orchestrator"] = {"ok": False, "error": str(e)[:200]}
        else:
            report["steps"]["agent_orchestrator"] = {"skip": True, "reason": "非交易日(maint) 跳过"}

        # 5.8) Agent 编排层 (P1-P3): AlphaLogics 因子发现 + FactorMAD 辩论 + 综合研判.
        #      仅在交易日 full 模式执行, 不阻断主流程.
        #      替代原有的分散 llm_commentary + pre_drl_brief 串联, 提供统一证据收集和工具调度.
        if mode == "full":
            try:
                from agent_orchestrator import post_market_analysis
                report["steps"]["agent_orchestrator"] = post_market_analysis(
                    day, use_llm=True)
            except Exception as e:
                report["steps"]["agent_orchestrator"] = {"ok": False,
                                                         "error": str(e)[:200]}
        else:
            report["steps"]["agent_orchestrator"] = {"skip": True,
                                                     "reason": "非交易日(maint) 跳过"}

        # 5.9) ic_curve 刷新: 打通 weight_optimizer 的实时 ICIR 依据.
        #      权重自适应(5.9 feedback)读 ic_curve_<f>_k20.csv 的 ic_h20 算 ICIR.
        #      ic_curve 由本步维护: 交易日增量合并(~2s 追平最新结算日), 周末全量重建(~3min).
        #      full 模式跑增量; 仅有 maint 模式跑全量(weight_optimizer 依赖的 alpha 因子).
        #      ic_curve 缺文件时增量无法合并, 自动回退全量重建一次.
        try:
            from ic_curve_refresh import refresh as ic_curve_refresh
            need_full = (mode == "maint")
            if not need_full:
                from config import DATA_DIR as _DD
                import os as _os
                _icd = _os.path.join(_DD, "ic")
                if not (_os.path.exists(_os.path.join(_icd, "ic_curve_vol_k20.csv"))
                        and _os.path.exists(_os.path.join(_icd, "ic_curve_mom_20_k20.csv"))):
                    need_full = True
            _iccr = ic_curve_refresh(full=need_full)  # vol/mom_rev
            report["steps"]["ic_curve_refresh"] = {
                "mode": "full" if need_full else "incremental",
                "ok": _iccr.get("ok"),
                "factors": {k: {m: v for m, v in e.items() if m in
                                ("mode", "days", "new_samples", "file", "error")}
                            for k, e in _iccr.get("factors", {}).items()},
            }
        except Exception as e:
            report["steps"]["ic_curve_refresh"] = {"ok": False, "error": str(e)[:200]}

        # 6) D->B 反馈闭环: 回算当日信号 IC -> 依 ICIR 自适应刷新因子权重.
        #    异常不阻断主流程, 记入回执.
        report["steps"]["feedback"] = self_closed_loop(day, day_dir, db)

        # 6.5) 增量学习闭环: 退化检测 + (P0/P1 触发) LLM 调参 + 写 reward_config.
        #     异常一律容错, 不阻断主流程.
        try:
            from incremental_learn import run_incremental_learn, set_reward_weights
            inc = run_incremental_learn(day, days=10, trigger_threshold="P1")
            report["steps"]["incremental_learn"] = {
                "triggered": inc.get("triggered"),
                "samples_n": inc.get("samples_n"),
                "worst_level": inc.get("degradation_worst_level"),
                "overall_score": inc.get("degradation_overall_score"),
                "reason": inc.get("reason"),
                "error": inc.get("error"),
            }
            # 若 LLM 触发并给出 reward_rebalance, 写入 reward_config
            if inc.get("triggered") and inc.get("optimization"):
                rr = inc["optimization"].get("reward_rebalance") or {}
                vw = rr.get("vnpy_weight")
                iw = rr.get("ic_weight")
                if vw is not None and iw is not None:
                    rationale = rr.get("rationale") or "incremental_learn triggered"
                    set_reward_weights(float(vw), float(iw),
                                       source="incremental_learn",
                                       rationale=rationale)
                    report["steps"]["incremental_learn"]["reward_config_written"] = True
        except Exception as e:
            report["steps"]["incremental_learn"] = {"error": str(e)[:200]}

        # 1.0989) 基础因子库日频验证 (可选, 放所有数据更新/因子计算之后).
        #       验证数据管道通畅性, 计算全部 24 个因子覆盖率和统计值, 非阻断.
        #       启用: 设置环境变量 FACTOR_LIBRARY_VERIFY=1
        try:
            import os as _os_fl
            if _os_fl.environ.get("FACTOR_LIBRARY_VERIFY", "0") == "1":
                from factor_library import verify_pipeline
                report["steps"]["factor_library_verify"] = verify_pipeline(day)
            else:
                report["steps"]["factor_library_verify"] = {"skip": True,
                    "reason": "未启用 (设置 FACTOR_LIBRARY_VERIFY=1 开启)"}
        except Exception as e:
            report["steps"]["factor_library_verify"] = {"ok": False,
                                                        "error": str(e)[:200]}

        # 1.0990) 收尾告警: gp4 提级门槛判定 (放在所有数据更新/因子计算/验证/落盘之后).
        #       最近20个"有分覆盖"成熟样本日 fwd5_ic>0 且 胜率>55% 连续 3 个交易日
        #       达标 -> GP4_READY 告警(pending_alert 去重, 人工 ack 或转负自动复位);
        #       只读+状态文件, 非阻断。
        try:
            from gp4_daily_validate import promotion_alert_check
            report["steps"]["gp4_promotion_alert"] = promotion_alert_check()
        except Exception as e:
            report["steps"]["gp4_promotion_alert"] = {"ok": False,
                                                      "error": str(e)[:200]}

        # 1.0991) AI Factor Lab 批量假设评估 (可选, 仅 full 模式).
        #       读取 hypotheses.json 中的因子假设, 批量评估并保存报告.
        #       启用: 设置环境变量 AI_FACTOR_LAB_ENABLE=1 且存在 hypotheses.json
        try:
            import os as _os_fl2
            _hyp_path = os.path.join(DATA_DIR, "factor_mine", "hypotheses.json")
            if mode == "full" and _os_fl2.environ.get("AI_FACTOR_LAB_ENABLE", "0") == "1":
                if os.path.exists(_hyp_path):
                    from ai_factor_lab import batch_evaluate, generate_report
                    import json as _json
                    _hyps = _json.load(open(_hyp_path, encoding="utf-8"))
                    _results = batch_evaluate(_hyps, end_day=day)
                    _report_path = os.path.join(DATA_DIR, "factor_mine", "ai_factor_lab_report.json")
                    os.makedirs(os.path.dirname(_report_path), exist_ok=True)
                    _json.dump(_results, open(_report_path, "w", encoding="utf-8"),
                               ensure_ascii=False, indent=2, default=str)
                    _n_adopt = sum(1 for r in _results if r.get("verdict") == "ADOPT")
                    report["steps"]["ai_factor_lab"] = {
                        "ok": True, "n_hypotheses": len(_hyps),
                        "n_adopt": _n_adopt, "report": _report_path}
                else:
                    report["steps"]["ai_factor_lab"] = {"skip": True,
                        "reason": f"hypotheses.json 不存在 ({_hyp_path})"}
            else:
                report["steps"]["ai_factor_lab"] = {"skip": True,
                    "reason": "未启用 (设置 AI_FACTOR_LAB_ENABLE=1 且 hypotheses.json 存在)"}
        except Exception as e:
            report["steps"]["ai_factor_lab"] = {"ok": False, "error": str(e)[:200]}

        # 1.0992) GP 日频因子自动挖掘 (可选, 仅 full 模式, 低频执行).
        #       运行 GP 引擎进化可读因子公式, 保存候选到报告.
        #       启用: 设置环境变量 GP_MINE_ENABLE=1
        #       注意: GP 挖掘计算密集, 建议每周运行 1-2 次而非每日.
        try:
            import os as _os_fl3
            if mode == "full" and _os_fl3.environ.get("GP_MINE_ENABLE", "0") == "1":
                from factor_mine.gp_mine_daily import run_mine
                _gp_r = run_mine(end_day=day, days=90, pop=50, gen=3, add_rolling=False)
                report["steps"]["gp_mining"] = {
                    "ok": _gp_r.get("ok", False),
                    "n_rows": _gp_r.get("params", {}).get("n_rows"),
                    "n_features": _gp_r.get("params", {}).get("n_features"),
                    "n_months": _gp_r.get("params", {}).get("n_months"),
                    "top_candidates": [
                        {"rank": c["rank"], "ic": round(c["fitness"], 5), "expr": c["readable"][:80]}
                        for c in (_gp_r.get("top_candidates") or [])[:5]
                    ],
                    "elapsed_s": _gp_r.get("elapsed_s"),
                    "report": os.path.join(DATA_DIR, "factor_mine", "gp_daily_mine_report.json"),
                }
            else:
                report["steps"]["gp_mining"] = {"skip": True,
                    "reason": "未启用 (设置 GP_MINE_ENABLE=1 开启, 建议每周1-2次)"}
        except Exception as e:
            report["steps"]["gp_mining"] = {"ok": False, "error": str(e)[:200]}

        report["status"] = "OK"
        report["输出文件"] = out_path
        # 最终回写，确保绩效归因与反馈结果也持久化到当天回执。
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        import traceback
        traceback.print_exc()
        report["status"] = f"ERROR: {e}"
    finally:
        db.close()

    return report


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="日频收盘 / 非交易日维护管道")
    ap.add_argument("--maint", action="store_true",
                    help="非交易日维护模式: 仅数据拉取+模型训练, 跳过选股/盘中/归档/绩效")
    ap.add_argument("--date", default=None, help="目标日期 YYYY-MM-DD (默认今天)")
    ap.add_argument("day", nargs="?", default=None,
                    help="(兼容旧用法) 目标日期 YYYY-MM-DD")
    a = ap.parse_args()
    day = a.date or a.day
    r = run_daily(day, mode="maint" if a.maint else "full")
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))