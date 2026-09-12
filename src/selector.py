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


class RotationSelector:
    def __init__(self, db: StockDB, n: int = MAX_STOCKS):
        self.db = db
        self.n = n

    # ---------- f_ml 融合因子接入(容错: 失败即退化为原打分) ----------
    def _latest_bar_date(self) -> str:
        return pd.Timestamp.today().strftime("%Y-%m-%d")

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
        good = ~np.isnan(vals)
        ranks = np.full(len(scored), np.nan)
        if int(good.sum()) > 1:
            order = np.argsort(np.argsort(vals[good]))
            ranks[good] = order / (good.sum() - 1.0)
        w = min(max(float(FML_WEIGHT), 0.0), 0.5)
        for i, s in enumerate(scored):
            raw = fml.get(s["canon"])
            if raw is None or np.isnan(ranks[i]):
                continue
            base = s["score"]
            s["fml"] = round(float(raw), 5)
            s["fml_rank"] = round(float(ranks[i]), 4)
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
        scored.sort(key=lambda x: x["score"], reverse=True)
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
        W = selector_weights()

        # [m4 性能] h5i 批量预热: 一次性加载 hist_day 前整窗日线, 避免对数千标的
        # 逐条触发 h5i 全表扫描 (~1s/标的). 预热失败自动回退逐条 SQL (语义不变).
        try:
            self.db.prefetch_daily_bars(as_of=hist_day, n=200)
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
        scored.sort(key=lambda x: x["score"], reverse=True)
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