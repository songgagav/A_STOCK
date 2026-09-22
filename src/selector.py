# ============================================================
# selector.py -- 全A轮动选股器
# 流程: 取全A候选池 -> 过滤 -> 逐股算P08信号+打分 -> 排序 -> TopN持仓目标
# ============================================================

import os
import json
import traceback
import numpy as np
import pandas as pd

from config import (
    MAX_STOCKS, DATA_DIR, DAILY_DIR, SNAP_DIR
)
from db import StockDB, filter_universe
from p08_signal import (
    last_signal, trend_score, governance_score, liquidity_score,
)
from factor_library import selector_weights, score_factor, _pb_rev_score, _roe_score, _mf_net_score
from ml_fusion_bridge import FML_WEIGHT  # 兼容旧 import; f_ml 计算统一走 factor_fusion.fusion_or_fml


# ---- 横截面 rank percentile 归一化 ----
_RANK_FIELDS = ["vol", "mom_rev", "pb_rev", "roe", "mf_net"]


def fusion_trim_q() -> float:
    """融合分截尾比例 (环境变量 FUSION_TRIM_Q, 默认 0 = 关闭).

    背景与证据(2026-09-13, 见 docs/pit-valuation.md 第 15/16 条): 融合分的池内 RankIC
    为正(+0.1105) 且全截面 IC 为正, 但**池内最尖的 top10**(约前 0.5%)前向收益为负
    (-5.89% vs 池内其余 +3.73%, 最高 1% 分位 -1.71%) —— 秩相关为正、极端头部反转。
    对参与排序/掺入的融合分**剔除最高的 q 比例**后, 头部收益转正且稳健:
      纯融合口径  3/5/8/10/15% 均改善, 5% 最强(+16.65pp, 11/11 窗口改善, t=4.10)
      掺入口径    3%/5% 有效(+7.22/+6.27pp, t=3.34/3.10), 8% 起失效
    故默认关闭(=0), 推荐值 0.05。
    """
    try:
        q = float(os.environ.get("FUSION_TRIM_Q", "0") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return min(max(q, 0.0), 0.5)


def trim_top(values: np.ndarray, q: float):
    """把最高 q 比例的值置为 NaN. 返回 (trimmed, mask).

    q<=0 或样本过少时原样返回。用于"极端头部反转"的处理: 被截掉的标的在排序里
    被降到最低档(rank=0)而不是"跳过", 以保持 score 量纲一致。
    """
    v = np.asarray(values, dtype=float)
    if q <= 0 or v.size < 20:
        return v, np.zeros(v.shape, dtype=bool)
    finite = v[np.isfinite(v)]
    if finite.size < 20:
        return v, np.zeros(v.shape, dtype=bool)
    cut = float(np.quantile(finite, 1.0 - q))
    mask = v > cut
    out = v.copy()
    out[mask] = np.nan
    return out, mask


def _rank_normalize(scored: list, W: dict) -> None:
    """将 scored 中各因子的线性裁切值替换为横截面 rank percentile (0..1).

    线性裁切 (hi-raw)/(hi-lo) 压缩了分布尾部, 标准差 ~0.23;
    rank percentile 保留原始排序信息, 标准差 ~0.29, 区分度提升 ~26%.
    """
    n = len(scored)
    if n < 3:
        return
    for field in _RANK_FIELDS:
        if W.get(field, 0.0) <= 0:
            continue
        vals = np.array([s.get(field, 0.5) for s in scored], dtype=float)
        # 处理 NaN / 中性值
        valid = np.isfinite(vals)
        ranks = np.full(n, 0.5)
        if np.sum(valid) > 1:
            order = np.argsort(np.argsort(vals[valid]))
            ranks[valid] = order / (np.sum(valid) - 1.0)
        for i, s in enumerate(scored):
            s[field] = round(float(ranks[i]), 4)

    # 重算 composite score
    for s in scored:
        s["score"] = round(
            W["signal"] * np.clip(0.5 + 2.0 * s["signal"], 0.0, 1.0) +
            W["trend"] * (s["trend"] + 1.0) / 2.0 +
            W["govern"] * s["govern"] +
            W["liquidity"] * s["liquidity"] +
            sum(W.get(f, 0.0) * s.get(f, 0.5) for f in _RANK_FIELDS),
            4,
        )


def _rank_key(x: dict):
    """排序键: 开启融合排名(RANK_BY_FUSION!=0)时用 fusion_rank_key, 否则用 score.

    _apply_fusion_rank 要么给**所有**条目写入 fusion_rank_key, 要么一条都不写,
    因此排序时量纲始终一致(不会混用 0~1 分位与原始 score)。
    """
    if "fusion_rank_key" in x:
        return x["fusion_rank_key"]
    return x["score"]


class RotationSelector:
    def __init__(self, db: StockDB, n: int = MAX_STOCKS):
        self.db = db
        self.n = n

    # ---------- f_ml 融合因子接入(容错: 失败即退化为原打分) ----------
    def _latest_bar_date(self) -> str:
        return pd.Timestamp.today().strftime("%Y-%m-%d")

    def _apply_fusion_rank(self, scored: list, as_of: str) -> None:
        """(2026-09-13) 可选: 让四因子融合分参与排序, 而非只用旧 SCORE_WEIGHTS 复合.

        背景(证据见 scripts/ic_neutral_check.py / docs/pit-valuation.md 第 10 条):
          旧复合 `signal` 的 IC 全视界为负(均值 -0.0674);
          而 pb_inv+ep+ocf_ps+roe_yy_chg 融合分 IC 全视界为正(修正 roe 方向后 +0.1167)。
        融合分目前只用于权重分配, 未参与选股排名。本函数提供开关:

          RANK_BY_FUSION=0 关闭(默认, 原行为不变)
          RANK_BY_FUSION=1 打开; 混合比例 FUSION_RANK_ALPHA (0=纯旧排序, 1=纯融合)

        实现要点: 只写 `fusion_rank_key`(两路排名各转 0~1 分位后线性混合), **不改
        `score`**, 以免影响下游 allocate_target_weights 等对 score 量纲的依赖;
        排序处优先用 fusion_rank_key。失败静默回退原排序。
        """
        if os.environ.get("RANK_BY_FUSION", "0") in ("", "0") or not scored:
            return
        try:
            alpha = float(os.environ.get("FUSION_RANK_ALPHA", "1.0"))
        except Exception:
            alpha = 1.0
        alpha = min(max(alpha, 0.0), 1.0)
        try:
            from factor_fusion import cross_section_scores
            syms = [str(x.get("canon") or "").split(".")[0].zfill(6) for x in scored]
            res = cross_section_scores(as_of, symbols=syms)
            zmap = (res or {}).get("scores") or {}
            if not zmap:
                return
            zs = np.array([zmap.get(s, np.nan) for s in syms], dtype=float)
            if int(np.isfinite(zs).sum()) < 30:
                return
            med = float(np.nanmedian(zs))
            zs = np.where(np.isfinite(zs), zs, med)
            # 极端头部截尾 (FUSION_TRIM_Q, 默认 0=关闭): 与 _blend_fml 同一口径,
            # 被截掉的标的降到最低分位, 不再参与头部竞争。
            zs_t, trimmed = trim_top(zs, fusion_trim_q())
            if trimmed.any():
                zs_t = np.where(trimmed, float(np.nanmin(zs)), zs_t)
            old = np.array([float(x.get("score") or 0.0) for x in scored], dtype=float)

            def _pct_rank(a: np.ndarray) -> np.ndarray:
                o = np.argsort(np.argsort(a))
                return o / max(len(a) - 1, 1)

            blended = (1.0 - alpha) * _pct_rank(old) + alpha * _pct_rank(zs_t)
            for x, b, z in zip(scored, blended, zs):
                x["fusion_rank_key"] = float(b)
                x["fusion_z"] = float(z)
        except Exception:
            return

    def _blend_fml(self, scored: list, as_of: str) -> None:
        """对 scored(含 canon/score) 现算 f_ml/fused score, 以截面百分位排名(0..1)
        小权重融合. 统一走 factor_fusion.fusion_or_fml (内部自动回退旧 compute_fml).
        任意异常/空结果一律静默跳过, 保持原打分不变(不写 fml 字段)."""
        if not scored:
            return
        try:
            from factor_fusion import fusion_or_fml
            fml, _used = fusion_or_fml(scored, as_of)
        except Exception:  # noqa: BLE001
            return
        if not fml:
            return
        vals = np.array([fml.get(s["canon"], np.nan) for s in scored],
                        dtype=float)
        # 极端头部截尾 (FUSION_TRIM_Q, 默认 0=关闭): 被截掉的标的 rank 记 0(最低档),
        # 即不再获得融合加成; 而不是整条跳过, 以保持 score 量纲与其余标的一致。
        q = fusion_trim_q()
        vals_t, trimmed = trim_top(vals, q)
        good = ~np.isnan(vals_t)
        ranks = np.full(len(scored), np.nan)
        if int(good.sum()) > 1:
            order = np.argsort(np.argsort(vals_t[good]))
            ranks[good] = order / (good.sum() - 1.0)
        if trimmed.any():
            ranks[trimmed] = 0.0
        w = min(max(float(FML_WEIGHT), 0.0), 0.5)
        for i, s in enumerate(scored):
            raw = fml.get(s["canon"])
            if raw is None or np.isnan(ranks[i]):
                continue
            base = s["score"]
            s["fml"] = round(float(raw), 5)
            s["fml_rank"] = round(float(ranks[i]), 4)
            if trimmed[i]:
                s["fml_trimmed"] = True      # 审计标记: 因极端头部被截尾而降档
            s["score"] = round((1.0 - w) * base + w * float(ranks[i]), 4)
            if ranks[i] >= 0.7:
                s["thesis"] = ("融合预测信号强" + ("；" + s["thesis"]
                               if s["thesis"] else ""))

    # ---------- 核心: 生成 TopN 持仓目标 ----------
    def select(self, real_time_prices: dict = None, as_of: str = None,
               hist_day: "str|None" = None) -> dict:
        """返回选股结果 dict:
        {
          'date': str, 'as_of': str,
          'universe_total': int, 'filtered': int, 'scored': int,
          'top_n': [...], 'basket_signal': float, 'guard': {...}
        }
        top_n item: {canon, name, price, signal, trend, govern,
                     liquidity, score, weights分配}

        real_time_prices: 可选 {canon: float}. 提供时视为"盘中重选":
            把每只股票最后一根日K的 close/high/low 替换为实时价后重算全部因子,
            price 字段也用实时价。这样午间重排会因最新价变化产生真实分化。
            (盘中当日K未落地, 用实时价模拟"当日K走完", 因此 P08/趋势/波动会实时刷新.)
        as_of:        可选时间戳, 用于午间重选快照标注.
        hist_day:     P4 no-lookahead 回测用. 指定历史交易日(YYYY-MM-DD)时,
                      只用该日及之前的数据选股: 候选池取自当时已上市且有日线的
                      标的, 因子/K线/财务全都截断到 hist_day, 杜绝未来数据前视.
        """
        if hist_day is not None:
            return self._select_hist(hist_day)

        uni = self.db.get_universe()
        if uni.empty:
            return {"error": "empty universe", "universe_total": 0}

        univ_total = len(uni)
        pool = filter_universe(uni)
        filtered = len(pool)

        # [m4 性能] h5i 批量预热 (语义不变, 仅加速逐 symbol 批量取 K 线)
        try:
            self.db.prefetch_daily_bars(as_of=None, n=200)
        except Exception:
            pass

        # [2026-09-22 性能] valuation/financials 的**全市场最新一期**批量预热。
        #
        # 为什么必须做: 下面的 scoring 循环里每只都要 `get_valuation` + `get_financials`,
        # 而两者在 h5i 上都是"扫整表取最新一期" —— 实测每次约 200ms, 而瓶颈是
        # **扫过 1540 万行本身**(双 CAST / 单 CAST / 不 CAST 三种写法都 ~200ms,
        # 与排序、类型转换无关)。2809 只 ⇒ 约 **11 分钟**, 是日常管道最大的一笔固定开销。
        # 一次性取全市场最新一期只要 **11.5s / 5812 只**, 之后每只是字典查表。
        # 实测 25 只: 5.84s -> 0.011s(约 500x), 全池 10.9 分钟 -> 1.3 秒,
        # 且**逐字段等价**(已用 NaN-aware 比对核过 25 只)。
        #
        # 注: 这里**不能**靠按 symbol 的记忆化解决 —— pool 里 2809 个 canon 互不相同,
        # 每个 symbol 只被问一次 ⇒ 任何按 symbol 的缓存都必然全部未命中。
        # 该改的是**查询形态(逐条 -> 批量)**, 不是加缓存。
        try:
            self.db.prefetch_latest_snapshots()
        except Exception:
            pass

        # 权重: 优先 ICIR 自适应(weight_optimizer 生成的 weights.json), 无则静态
        W = selector_weights()

        scored = []
        max_amount = pool["amount"].astype(float).max() if len(pool) else 0.0
        n_signal_ok = 0

        for _, r in pool.iterrows():
            canon = r["canon"]
            bars = self.db.get_bars(canon, n=200)
            if len(bars) < 60:
                continue  # 历史数据不足
            if bars["close"].astype(float).iloc[-1] <= 0:
                continue

            live_px = None
            if real_time_prices:
                live_px = real_time_prices.get(canon)
                if live_px is not None and live_px > 0:
                    # 用实时价替换最新一根K: 模拟当日K线按实时价走完
                    bars = bars.copy()
                    prev_close = bars["close"].astype(float).iloc[-2] if len(bars) >= 2 else live_px
                    bars.iloc[-1, bars.columns.get_loc("close")] = live_px
                    bars.iloc[-1, bars.columns.get_loc("high")] = max(live_px, prev_close)
                    bars.iloc[-1, bars.columns.get_loc("low")] = min(live_px, prev_close)
                    # P1: 同步重算该日 change_pct, 使 build_adj_close 复权重建与实时价一致
                    if "change_pct" in bars.columns and prev_close and prev_close > 0:
                        bars.iloc[-1, bars.columns.get_loc("change_pct")] = \
                            (live_px / prev_close - 1.0) * 100.0

            try:
                sig = last_signal(bars)
                trend = trend_score(bars)
                # 实证 alpha 因子统一走 factor_library 注册表 (与 ic_backtest 同一份定义)
                vol = score_factor("vol", bars)
                mom_rev = score_factor("mom", bars)
            except Exception:
                continue
            # 治理/质量: 用 financials 表真实财务打分, 无财务数据回退估值快照占位
            fin = self.db.get_financials(canon)
            gov = governance_score(r.to_dict(), fin)
            gov_src = "fina" if fin else "snap"
            liq = liquidity_score(r.to_dict(), max_amount)

            # 新基本面因子: 估值/质量/资金流 (从 DB 读取, 不依赖日线K)
            val = self.db.get_valuation(canon)
            pb_rev = _pb_rev_score(val.get("pb") if val else None)
            roe_val = fin.get("roe") if fin else None
            if roe_val is not None:
                try:
                    roe_val = float(str(roe_val).replace("%", "")) / 100.0
                except (ValueError, TypeError):
                    roe_val = None
            roe_factor = _roe_score(roe_val)
            mf = self.db.get_money_flow(canon)
            mf_net = _mf_net_score(
                mf.get("net_flow") if mf else None,
                mf.get("amount") if mf else None,
            )

            # 综合打分: 信号主导(非线性放大区分度), 趋势/治理/流动性为辅
            #           低波动(vol)与反转动量(mom_rev)为实证alpha, 正向加分
            # signal 分量: 正信号越高分越高, 负信号压低分 (0..1)
            sig_part = np.clip(0.5 + 2.0 * sig, 0.0, 1.0)   # sig=-0.25->0, 0->0.5, +0.25->1
            trend_part = (trend + 1.0) / 2.0                     # 0..1
            # 只做多: 负信号不直接清仓但降低排名; 流动性做最小保障(向下压)
            score = (
                W["signal"] * sig_part +
                W["trend"] * trend_part +
                W["govern"] * gov +
                W["liquidity"] * liq +
                W["vol"] * vol +
                W["mom_rev"] * mom_rev +
                W["pb_rev"] * pb_rev +
                W["roe"] * roe_factor +
                W["mf_net"] * mf_net
            )
            n_signal_ok += 1
            # 入选理由: 逐分项归因, 便于事后审计因子贡献
            thesis_parts = []
            if sig >= 0.3:
                thesis_parts.append("P08强多信号({:+.2f})".format(float(sig)))
            elif sig >= 0.1:
                thesis_parts.append("P08弱多信号({:+.2f})".format(float(sig)))
            elif sig <= -0.3:
                thesis_parts.append("P08空信号({:+.2f})走弱排名".format(float(sig)))
            if trend >= 0.6:
                thesis_parts.append("强趋势")
            elif trend >= 0.4:
                thesis_parts.append("趋势转好")
            if gov >= 0.6:
                thesis_parts.append("高治理质量")
            if vol >= 0.7:
                thesis_parts.append("低波动")
            if mom_rev >= 0.7:
                thesis_parts.append("超跌反转")
            elif mom_rev <= 0.3:
                thesis_parts.append("高动量(反转减分)")
            if liq < 0.3:
                thesis_parts.append("流动性偏弱")
            thesis = "；".join(thesis_parts) if thesis_parts else "中性信号"
            scored.append({
                "canon": canon,
                "name": r.get("name6"),
                "price": round(float(live_px if live_px is not None and live_px > 0 else r["price"]), 3),
                "signal": round(float(sig), 4),
                "trend": round(float(trend), 3),
                "vol": round(float(vol), 3),
                "mom_rev": round(float(mom_rev), 3),
                "pb_rev": round(float(pb_rev), 3),
                "roe": round(float(roe_factor), 3),
                "mf_net": round(float(mf_net), 3),
                "govern": round(float(gov), 3),
                "gov_src": gov_src,
                "liquidity": round(float(liq), 3),
                "score": round(float(score), 4),
                "thesis": thesis,
                "float_mv_yi": float(r["float_mv"]),
                "pe_ttm": float(r["pe_ttm"]) if pd.notna(r["pe_ttm"]) else None,
                "pb": float(r["pb"]) if pd.notna(r["pb"]) else None,
            })

        # f_ml 融合因子: 可治理/容错的额外 alpha, 权重小起步; 失败则完全退化为原打分
        # [P3修复] rank 归一化: 线性裁切压缩了因子分散度(std~0.23),
        #   改用横截面 rank percentile (std~0.29) 保留更多信息.
        #   在 f_ml 融合前执行, 不改变因子内部逻辑, 仅替换归一化方式.
        if scored:
            _rank_normalize(scored, W)

        self._blend_fml(scored, as_of or self._latest_bar_date())
        self._apply_fusion_rank(scored, as_of or self._latest_bar_date())
        scored.sort(key=_rank_key, reverse=True)
        top_n = scored[: self.n]
        # 策略层优化 (2026-09-05): 目标权重分配 — f_ml 预测加权(收缩+集中度上限)
        # 写入每项 target_weight(相对总资产, 和=1); 引擎下单将消费该字段,
        # 关闭「target_weight 悬空」契约(健康检查长年告警项).
        if top_n:
            from target_weighting import allocate_target_weights
            allocate_target_weights(top_n)

        basket_signal = float(np.mean([t["signal"] for t in top_n])) if top_n else 0.0

        return {
            "date": None,   # 由调用方填
            "as_of": as_of,
            "universe_total": univ_total,
            "filtered": filtered,
            "scored": n_signal_ok,
            "top_n": top_n,
            "basket_signal": round(basket_signal, 4),
            # 全池信号快照: 保留"本轮算过分的所有标的"因分明细,
            # 用于事后 IC 衰减跟踪(对比后续收益), 而非仅存 TopN
            "pool_snapshot": scored,
        }

    # ---------- P4: 历史日 no-lookahead 选股 ----------
    def _select_hist(self, hist_day: str) -> dict:
        """在指定历史日 hist_day(YYYY-MM-DD)上用"当时可得"的数据复刻 select 全流程,
        消除前视偏差: 候选池/因子K线/财务全部截断到 hist_day, 绝不使用其后数据。

        governance 分: 用 as_of 财务(report_date<=hist_day)打分; 无财务时回退到
        bars 可得的占位。估值特征(pe_ttm/pb/ps_ttm/total_mv/float_mv)自
        2026-09-12 起由 valuation 表做严格 as-of 取值(<= hist_day, 窗口 400 天),
        因此 governance 的 PB/PE 负向过滤与 filter_universe 的流通市值过滤
        在历史路径同样生效, 与实盘读快照(valuation_snapshot)的行为对齐,
        不再存在"回测缺估值特征"的前视规避型分叉。
        """
        uni = self.db.get_universe_asof(hist_day)
        if uni.empty:
            return {"error": "empty universe", "universe_total": 0, "hist_day": hist_day}
        univ_total = len(uni)
        # P5: 次新 cutoff 以历史日 as_of 为基准(非"今天"), 不泄漏 as_of 之后
        # 才上市的标的, 与实盘"以当日为次新口径"保持一致。
        pool = filter_universe(uni, as_of=hist_day)
        filtered = len(pool)
        # 2026-09-13: 历史路径传 as_of -> 只用该日及之前的 IC 曲线判因子隔离,
        # 消除"用今天的 IC 给历史日期定权重"的因子权重层前视
        W = selector_weights(as_of=hist_day)

        # [m4 性能] h5i 批量预热: 一次性加载 hist_day 前整窗日线, 避免对数千标的
        # 逐条触发 h5i 全表扫描 (~1s/标的). 预热失败自动回退逐条 SQL (语义不变).
        try:
            self.db.prefetch_daily_bars(as_of=hist_day, n=200)
        except Exception:
            pass

        # [2026-09-22 no-lookahead 防护] **冻结"最新一期"快照**。
        # `prefetch_latest_snapshots()` 装的是每个 symbol 的**最新一行**(今天口径);
        # 历史回测若命中它 = 把今天的财报/估值喂给过去的选股 = 前视偏差。
        # 本路径当前走的是 `get_financials_asof(canon, hist_day)`(独立函数, 不读快照),
        # 故此冻结**不改变现有行为** —— 它是防止将来有人改动时静默引入前视的护栏。
        try:
            import db as _db
            _db._LATEST_SNAP_FROZEN["on"] = True
            _db._LATEST_SNAP["valuation"] = {}
            _db._LATEST_SNAP["financials"] = {}
        except Exception:
            pass

        scored = []
        max_amount = pool["amount"].astype(float).max() if len(pool) else 0.0
        n_signal_ok = 0
        for _, r in pool.iterrows():
            canon = r["canon"]
            try:
                bars = self.db.get_bars(canon, n=200, as_of=hist_day)
            except Exception:
                continue
            if bars is None or len(bars) < 60:
                continue
            if bars["close"].astype(float).iloc[-1] <= 0:
                continue
            try:
                sig = last_signal(bars)
                trend = trend_score(bars)
                vol = score_factor("vol", bars)
                mom_rev = score_factor("mom", bars)
            except Exception:
                continue
            fin = None
            try:
                fin = self.db.get_financials_asof(canon, hist_day)
            except Exception:
                fin = None
            gov = governance_score(r.to_dict(), fin)
            gov_src = "fina" if fin else "bars"
            liq = liquidity_score(r.to_dict(), max_amount)

            sig_part = np.clip(0.5 + 2.0 * sig, 0.0, 1.0)
            trend_part = (trend + 1.0) / 2.0
            score = (
                W["signal"] * sig_part + W["trend"] * trend_part +
                W["govern"] * gov + W["liquidity"] * liq +
                W["vol"] * vol + W["mom_rev"] * mom_rev
            )
            n_signal_ok += 1
            thesis_parts = []
            if sig >= 0.3:
                thesis_parts.append("P08强多信号({:+.2f})".format(float(sig)))
            elif sig >= 0.1:
                thesis_parts.append("P08弱多信号({:+.2f})".format(float(sig)))
            elif sig <= -0.3:
                thesis_parts.append("P08空信号({:+.2f})走弱排名".format(float(sig)))
            if trend >= 0.6:
                thesis_parts.append("强趋势")
            elif trend >= 0.4:
                thesis_parts.append("趋势转好")
            if gov >= 0.6:
                thesis_parts.append("高治理质量")
            if vol >= 0.7:
                thesis_parts.append("低波动")
            if mom_rev >= 0.7:
                thesis_parts.append("超跌反转")
            elif mom_rev <= 0.3:
                thesis_parts.append("高动量(反转减分)")
            if liq < 0.3:
                thesis_parts.append("流动性偏弱")
            thesis = "；".join(thesis_parts) if thesis_parts else "中性信号"
            scored.append({
                "canon": canon,
                "name": str(r.get("name6") or ""),
                "price": round(float(r["price"]), 3),
                "signal": round(float(sig), 4),
                "trend": round(float(trend), 3),
                "vol": round(float(vol), 3),
                "mom_rev": round(float(mom_rev), 3),
                "govern": round(float(gov), 3),
                "gov_src": gov_src,
                "liquidity": round(float(liq), 3),
                "score": round(float(score), 4),
                "thesis": thesis,
            })
        self._apply_fusion_rank(scored, hist_day)
        scored.sort(key=_rank_key, reverse=True)
        top_n = scored[: self.n]
        basket_signal = float(np.mean([t["signal"] for t in top_n])) if top_n else 0.0
        return {
            "date": None,
            "hist_day": hist_day,
            "universe_total": univ_total,
            "filtered": filtered,
            "scored": n_signal_ok,
            "top_n": top_n,
            "basket_signal": round(basket_signal, 4),
            "pool_snapshot": scored,
        }


def save_selection(result: dict, day: str) -> str:
    """把选股结果存到 data/daily/<day>/selection.json

    全池信号快照独立落 disk 为 pool_snapshot.json (供 IC 衰减回测),
    selection.json 只保留 TopN 与元信息, 避免单个文件过大。
    """
    d = os.path.join(DAILY_DIR, day)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "selection.json")

    snapshot = result.pop("pool_snapshot", None)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    if snapshot:
        pool_path = os.path.join(d, "pool_snapshot.json")
        with open(pool_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2, default=str)
    return path


if __name__ == "__main__":
    db = StockDB()
    try:
        r = RotationSelector(db).select()
        r["date"] = pd.Timestamp.today().strftime("%Y-%m-%d")
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
        print("\nSAVED:", save_selection(r, r["date"].replace("-", "")))
    finally:
        db.close()