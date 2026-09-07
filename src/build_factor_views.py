# ============================================================
# build_factor_views.py -- DuckDB 物化视图 + Parquet 导出
#
# 设计目标: 把 daily_bars / valuation / money_flow_estimate / northbound_money /
#          margin_daily / daily_bars 衍生指标预计算成 6 张因子视图, 落 DuckDB
#          物化表 + 同步导出 Parquet (供其他下游工具直接 read_parquet, 避免
#          重复大表 join).
#
# 视图清单:
#   1) v_factor_scores_daily    每只票 6 因子日分 (signal/trend/govern/liquidity/vol/mom_rev)
#   2) v_factor_ic_latest       各因子最近 20 日 IC, ICIR, win_rate
#   3) v_market_breadth         每日涨/跌/平家数 + 涨停/跌停家数 + 成交额
#   4) v_money_flow_rank        北向+主力资金按日 TopN (北向净买 / 主力净买)
#   5) v_signal_summary         最新一日全 A 技术信号汇总 (BUY/SELL/HOLD)
#   6) v_universe_snapshot      最新一日全 A 候选池 (含基础过滤 + 总分)
#
# 调用方:
#   - run_daily.py 在 db_update 之后, 选股之前调用
#   - 手动: python build_factor_views.py
#
# 输出:
#   - DuckDB 物化表: data/views/<view_name>   (duckdb 主库以 view 形式创建)
#   - Parquet:       data/views/parquet/<view_name>.parquet
#   - 元数据:        data/views/build_meta.json
# ============================================================

from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from config import DATA_DIR, DUCKDB_PATH  # noqa: E402

VIEWS_DIR = os.path.join(DATA_DIR, "views")
PARQUET_DIR = os.path.join(VIEWS_DIR, "parquet")
META_FILE = os.path.join(VIEWS_DIR, "build_meta.json")
# [2026-09-05 迁移] h5i 视图 parquet 目录 (新规范输出, 同时兼容写回旧路径)
H5I_MARKET_DB = os.path.join(DATA_DIR, "h5i", "market.db")
H5I_VIEWS_DIR = os.path.join(DATA_DIR, "h5i", "views")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# 共享过滤: 仅保留 A 股代码段 (剔除可转债/B股/基金/其他债务工具)
# ============================================================
# daily_bars.symbol 为裸 6 位代码 (无前缀), symbols.name 未填充, 只能用代码段辨识.
# 有效 A 股代码段: 深主板 000/001/002/003, 创业板 300/301/302,
#                 沪主板 600/601/603/605, 科创板 688/689.
# 剔除: 沪市可转债 110/111/113, 深市可转债 123/127/128,
#       深B股 200, 沪B股 900, 北交所 920/8xx/4xx, 其它非A段.
A_SHARE_CODE_FILTER = """(
    LEFT(symbol, 3) IN ('000','001','002','003','300','301','302',
                        '600','601','603','605','688','689')
)"""


# ============================================================
# 视图 1: 每只票 6 因子日分
# ============================================================
SQL_FACTOR_SCORES = r"""
CREATE OR REPLACE TABLE v_factor_scores_daily AS
WITH base AS (
    SELECT
        symbol AS canon,
        date,
        close, open, high, low, volume,
        -- 单点根治 NaN/Inf 污染: DuckDB 中 NaN=NaN 为 TRUE(见 ISNAN 验证), 自比较
        -- guard 挡不住 NaN; 且 AVG/COALESCE 都不替换 NaN(只替换 NULL).
        -- 故此处把源数值列的 NaN/Inf 统一规范化为 NULL, 让 AVG 跳过 / COALESCE 兜底,
        -- 从而 mom_20, vol_20, f_govern, f_liquidity 全部自动安全 (2026-09-02 修复).
        CASE WHEN amount IS NULL OR ISNAN(amount)   OR ISINF(amount)   THEN NULL ELSE amount   END AS amount,
        CASE WHEN change_pct IS NULL OR ISNAN(change_pct) OR ISINF(change_pct) THEN NULL ELSE change_pct END AS change_pct,
        CASE WHEN turnover IS NULL OR ISNAN(turnover) OR ISINF(turnover) THEN NULL ELSE turnover END AS turnover
    FROM daily_bars
    WHERE date >= (SELECT MAX(date) - 120 FROM daily_bars)
      AND """ + A_SHARE_CODE_FILTER + """
),
mom AS (
    SELECT canon, date,
        AVG(change_pct) OVER (PARTITION BY canon ORDER BY date
            ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS mom_20,
        -- 避开 DuckDB STDDEV_SAMP 在含 NULL 列上抛 OutOfRange 的已知 bug,
        -- 手写样本标准差: sqrt(E[X^2] - (E[X])^2), 配合 NULL 处理.
        SQRT(GREATEST(
            COALESCE(AVG(change_pct * change_pct) OVER (PARTITION BY canon ORDER BY date
                ROWS BETWEEN 19 PRECEDING AND CURRENT ROW), 0.0)
            - COALESCE(AVG(change_pct) OVER (PARTITION BY canon ORDER BY date
                ROWS BETWEEN 19 PRECEDING AND CURRENT ROW), 0.0)
              * COALESCE(AVG(change_pct) OVER (PARTITION BY canon ORDER BY date
                ROWS BETWEEN 19 PRECEDING AND CURRENT ROW), 0.0),
            0.0
        )) AS vol_20,
        AVG(amount) OVER (PARTITION BY canon ORDER BY date
            ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS amt_20
    FROM base
),
trend AS (
    SELECT canon, date,
        AVG(close) OVER (PARTITION BY canon ORDER BY date
            ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS sma_20,
        AVG(close) OVER (PARTITION BY canon ORDER BY date
            ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS sma_60
    FROM base
),
signal AS (
    SELECT canon, date,
        -- 简化信号: 突破 + 量比, 把当 close > sma_20 且 turnover > 1.0 视为正向
        CASE WHEN close > sma_20 AND close > sma_60 THEN 1.0 ELSE 0.0 END AS trend_up,
        CASE WHEN close < sma_20 AND close < sma_60 THEN 1.0 ELSE 0.0 END AS trend_dn
    FROM base
    JOIN trend USING (canon, date)
)
SELECT
    b.canon, b.date,
    -- 因子 1: signal (综合趋势 + 量)
    (s.trend_up - s.trend_dn) * COALESCE(b.turnover, 0) AS f_signal,
    -- 因子 2: trend (MA 多头排列)
    CASE
        WHEN b.close > t.sma_20 AND t.sma_20 > t.sma_60 THEN 1.0
        WHEN b.close < t.sma_20 AND t.sma_20 < t.sma_60 THEN -1.0
        ELSE 0.0
    END AS f_trend,
    -- 因子 3: govern (用 change_pct 稳定性近似; 越稳越好 -> 1 - |change_pct|/10)
    GREATEST(-1.0, LEAST(1.0, 1.0 - COALESCE(ABS(b.change_pct), 0) / 10.0)) AS f_govern,
    -- 因子 4: liquidity (用 amt_20 在全市场百分位, 这里简化为 turnover)
    COALESCE(b.turnover, 0) AS f_liquidity,
    -- 因子 5: vol (低波动 = 正向, 用 vol_20 反向)
    CASE WHEN m.vol_20 IS NULL OR m.vol_20 = 0
              OR ISNAN(m.vol_20) OR ISINF(m.vol_20) THEN 0.0
         ELSE 1.0 - LEAST(1.0, m.vol_20 / 5.0) END AS f_vol,
    -- 因子 6: mom_rev (反转 = -mom_20). 源 change_pct 已在上游 base 规范化(Inf/NaN->NULL),
    -- 此处再以 ISNAN/ISINF 双保险兜底: DuckDB 中 NaN=NaN 为 TRUE, 自比较 guard 无效 (2026-09-02)
    - (CASE WHEN m.mom_20 IS NOT NULL AND NOT ISNAN(m.mom_20) AND NOT ISINF(m.mom_20)
           THEN COALESCE(m.mom_20, 0) ELSE 0.0 END) / 5.0 AS f_mom_rev
FROM base b
LEFT JOIN mom m USING (canon, date)
LEFT JOIN trend t USING (canon, date)
LEFT JOIN signal s USING (canon, date)
"""


# ============================================================
# 视图 2: 各因子最近 20 日 IC
# ============================================================
SQL_FACTOR_IC_LATEST = r"""
CREATE OR REPLACE TABLE v_factor_ic_latest AS
WITH ret AS (
    SELECT symbol AS canon, date,
        -- close<=0(停牌/数据缺失) 时 LEAD(close)/close=Inf 会污染当日 IC,
        -- 用 CASE 返回 NULL, 由下方 WHERE IS NOT NULL 过滤异常股 (2026-09-02 修复)
        CASE WHEN close > 0
                  AND LEAD(close) OVER (PARTITION BY symbol ORDER BY date) > 0
             THEN LEAD(close) OVER (PARTITION BY symbol ORDER BY date) / close - 1.0
             END AS fwd_1d
    FROM daily_bars
    WHERE date >= (SELECT MAX(date) - 60 FROM daily_bars)
),
joined AS (
    SELECT v.canon, v.date,
        v.f_signal, v.f_trend, v.f_govern, v.f_liquidity, v.f_vol, v.f_mom_rev,
        r.fwd_1d
    FROM v_factor_scores_daily v
    LEFT JOIN ret r USING (canon, date)
    WHERE r.fwd_1d IS NOT NULL AND NOT ISNAN(r.fwd_1d)  -- 兜底: NaN 非 NULL, 需显式过滤
),
ic_per_day AS (
    SELECT date,
        -- DuckDB 的 corr()/STDDEV_POP/VAR_POP 在 NULL 列上抛 OutOfRange.
        -- 完全手写: cov = E[xy] - E[x]E[y]; var = E[x^2] - (E[x])^2;
        -- corr = cov / (sqrt(var_x) * sqrt(var_y)), 各项用 COALESCE(NULL, 0.0) 兜底.
        -- 但注意 NULL * NULL = NULL, AVG 会跳过 NULL, 所以 E[xy] 在大量 NULL 时也安全.
        (COALESCE(AVG(f_signal * fwd_1d), 0.0)
         - COALESCE(AVG(f_signal), 0.0) * COALESCE(AVG(fwd_1d), 0.0))
        / NULLIF(
            SQRT(GREATEST(
                COALESCE(AVG(f_signal * f_signal), 0.0)
                - COALESCE(AVG(f_signal), 0.0) * COALESCE(AVG(f_signal), 0.0),
                0.0))
            * SQRT(GREATEST(
                COALESCE(AVG(fwd_1d * fwd_1d), 0.0)
                - COALESCE(AVG(fwd_1d), 0.0) * COALESCE(AVG(fwd_1d), 0.0),
                0.0)),
            0.0) AS ic_signal,
        (COALESCE(AVG(f_trend * fwd_1d), 0.0)
         - COALESCE(AVG(f_trend), 0.0) * COALESCE(AVG(fwd_1d), 0.0))
        / NULLIF(
            SQRT(GREATEST(
                COALESCE(AVG(f_trend * f_trend), 0.0)
                - COALESCE(AVG(f_trend), 0.0) * COALESCE(AVG(f_trend), 0.0),
                0.0))
            * SQRT(GREATEST(
                COALESCE(AVG(fwd_1d * fwd_1d), 0.0)
                - COALESCE(AVG(fwd_1d), 0.0) * COALESCE(AVG(fwd_1d), 0.0),
                0.0)),
            0.0) AS ic_trend,
        (COALESCE(AVG(f_govern * fwd_1d), 0.0)
         - COALESCE(AVG(f_govern), 0.0) * COALESCE(AVG(fwd_1d), 0.0))
        / NULLIF(
            SQRT(GREATEST(
                COALESCE(AVG(f_govern * f_govern), 0.0)
                - COALESCE(AVG(f_govern), 0.0) * COALESCE(AVG(f_govern), 0.0),
                0.0))
            * SQRT(GREATEST(
                COALESCE(AVG(fwd_1d * fwd_1d), 0.0)
                - COALESCE(AVG(fwd_1d), 0.0) * COALESCE(AVG(fwd_1d), 0.0),
                0.0)),
            0.0) AS ic_govern,
        (COALESCE(AVG(f_liquidity * fwd_1d), 0.0)
         - COALESCE(AVG(f_liquidity), 0.0) * COALESCE(AVG(fwd_1d), 0.0))
        / NULLIF(
            SQRT(GREATEST(
                COALESCE(AVG(f_liquidity * f_liquidity), 0.0)
                - COALESCE(AVG(f_liquidity), 0.0) * COALESCE(AVG(f_liquidity), 0.0),
                0.0))
            * SQRT(GREATEST(
                COALESCE(AVG(fwd_1d * fwd_1d), 0.0)
                - COALESCE(AVG(fwd_1d), 0.0) * COALESCE(AVG(fwd_1d), 0.0),
                0.0)),
            0.0) AS ic_liquidity,
        (COALESCE(AVG(f_vol * fwd_1d), 0.0)
         - COALESCE(AVG(f_vol), 0.0) * COALESCE(AVG(fwd_1d), 0.0))
        / NULLIF(
            SQRT(GREATEST(
                COALESCE(AVG(f_vol * f_vol), 0.0)
                - COALESCE(AVG(f_vol), 0.0) * COALESCE(AVG(f_vol), 0.0),
                0.0))
            * SQRT(GREATEST(
                COALESCE(AVG(fwd_1d * fwd_1d), 0.0)
                - COALESCE(AVG(fwd_1d), 0.0) * COALESCE(AVG(fwd_1d), 0.0),
                0.0)),
            0.0) AS ic_vol,
        (COALESCE(AVG(f_mom_rev * fwd_1d), 0.0)
         - COALESCE(AVG(f_mom_rev), 0.0) * COALESCE(AVG(fwd_1d), 0.0))
        / NULLIF(
            SQRT(GREATEST(
                COALESCE(AVG(f_mom_rev * f_mom_rev), 0.0)
                - COALESCE(AVG(f_mom_rev), 0.0) * COALESCE(AVG(f_mom_rev), 0.0),
                0.0))
            * SQRT(GREATEST(
                COALESCE(AVG(fwd_1d * fwd_1d), 0.0)
                - COALESCE(AVG(fwd_1d), 0.0) * COALESCE(AVG(fwd_1d), 0.0),
                0.0)),
            0.0) AS ic_mom_rev
    FROM joined
    GROUP BY date
)
SELECT
    factor, ic_value, mean_20, std_20, icir_20, win_rate_20, n_days
FROM (
    SELECT 'signal' AS factor,
        COALESCE(AVG(ic_signal), 0.0) AS ic_value,
        COALESCE(AVG(ic_signal), 0.0) AS mean_20,
        -- DuckDB STDDEV_SAMP 在 NULL 列上抛 OutOfRange, 手写 std_20 = sqrt(E[X^2] - (E[X])^2)
        SQRT(GREATEST(
            COALESCE(AVG(ic_signal * ic_signal), 0.0)
            - COALESCE(AVG(ic_signal), 0.0) * COALESCE(AVG(ic_signal), 0.0),
            0.0)) AS std_20,
        COALESCE(AVG(ic_signal), 0.0)
        / NULLIF(SQRT(GREATEST(
            COALESCE(AVG(ic_signal * ic_signal), 0.0)
            - COALESCE(AVG(ic_signal), 0.0) * COALESCE(AVG(ic_signal), 0.0),
            0.0)), 0.0) AS icir_20,
        AVG(CASE WHEN ic_signal > 0 THEN 1.0 ELSE 0.0 END) AS win_rate_20,
        COUNT(*) AS n_days
    FROM (SELECT * FROM ic_per_day ORDER BY date DESC LIMIT 20)
    UNION ALL
    SELECT 'trend', COALESCE(AVG(ic_trend), 0.0), COALESCE(AVG(ic_trend), 0.0),
        SQRT(GREATEST(
            COALESCE(AVG(ic_trend * ic_trend), 0.0)
            - COALESCE(AVG(ic_trend), 0.0) * COALESCE(AVG(ic_trend), 0.0),
            0.0)),
        COALESCE(AVG(ic_trend), 0.0)
        / NULLIF(SQRT(GREATEST(
            COALESCE(AVG(ic_trend * ic_trend), 0.0)
            - COALESCE(AVG(ic_trend), 0.0) * COALESCE(AVG(ic_trend), 0.0),
            0.0)), 0.0),
        AVG(CASE WHEN ic_trend > 0 THEN 1.0 ELSE 0.0 END),
        COUNT(*)
    FROM (SELECT * FROM ic_per_day ORDER BY date DESC LIMIT 20)
    UNION ALL
    SELECT 'govern', COALESCE(AVG(ic_govern), 0.0), COALESCE(AVG(ic_govern), 0.0),
        SQRT(GREATEST(
            COALESCE(AVG(ic_govern * ic_govern), 0.0)
            - COALESCE(AVG(ic_govern), 0.0) * COALESCE(AVG(ic_govern), 0.0),
            0.0)),
        COALESCE(AVG(ic_govern), 0.0)
        / NULLIF(SQRT(GREATEST(
            COALESCE(AVG(ic_govern * ic_govern), 0.0)
            - COALESCE(AVG(ic_govern), 0.0) * COALESCE(AVG(ic_govern), 0.0),
            0.0)), 0.0),
        AVG(CASE WHEN ic_govern > 0 THEN 1.0 ELSE 0.0 END),
        COUNT(*)
    FROM (SELECT * FROM ic_per_day ORDER BY date DESC LIMIT 20)
    UNION ALL
    SELECT 'liquidity', COALESCE(AVG(ic_liquidity), 0.0), COALESCE(AVG(ic_liquidity), 0.0),
        SQRT(GREATEST(
            COALESCE(AVG(ic_liquidity * ic_liquidity), 0.0)
            - COALESCE(AVG(ic_liquidity), 0.0) * COALESCE(AVG(ic_liquidity), 0.0),
            0.0)),
        COALESCE(AVG(ic_liquidity), 0.0)
        / NULLIF(SQRT(GREATEST(
            COALESCE(AVG(ic_liquidity * ic_liquidity), 0.0)
            - COALESCE(AVG(ic_liquidity), 0.0) * COALESCE(AVG(ic_liquidity), 0.0),
            0.0)), 0.0),
        AVG(CASE WHEN ic_liquidity > 0 THEN 1.0 ELSE 0.0 END),
        COUNT(*)
    FROM (SELECT * FROM ic_per_day ORDER BY date DESC LIMIT 20)
    UNION ALL
    SELECT 'vol', COALESCE(AVG(ic_vol), 0.0), COALESCE(AVG(ic_vol), 0.0),
        SQRT(GREATEST(
            COALESCE(AVG(ic_vol * ic_vol), 0.0)
            - COALESCE(AVG(ic_vol), 0.0) * COALESCE(AVG(ic_vol), 0.0),
            0.0)),
        COALESCE(AVG(ic_vol), 0.0)
        / NULLIF(SQRT(GREATEST(
            COALESCE(AVG(ic_vol * ic_vol), 0.0)
            - COALESCE(AVG(ic_vol), 0.0) * COALESCE(AVG(ic_vol), 0.0),
            0.0)), 0.0),
        AVG(CASE WHEN ic_vol > 0 THEN 1.0 ELSE 0.0 END),
        COUNT(*)
    FROM (SELECT * FROM ic_per_day ORDER BY date DESC LIMIT 20)
    UNION ALL
    SELECT 'mom_rev', COALESCE(AVG(ic_mom_rev), 0.0), COALESCE(AVG(ic_mom_rev), 0.0),
        SQRT(GREATEST(
            COALESCE(AVG(ic_mom_rev * ic_mom_rev), 0.0)
            - COALESCE(AVG(ic_mom_rev), 0.0) * COALESCE(AVG(ic_mom_rev), 0.0),
            0.0)),
        COALESCE(AVG(ic_mom_rev), 0.0)
        / NULLIF(SQRT(GREATEST(
            COALESCE(AVG(ic_mom_rev * ic_mom_rev), 0.0)
            - COALESCE(AVG(ic_mom_rev), 0.0) * COALESCE(AVG(ic_mom_rev), 0.0),
            0.0)), 0.0),
        AVG(CASE WHEN ic_mom_rev > 0 THEN 1.0 ELSE 0.0 END),
        COUNT(*)
    FROM (SELECT * FROM ic_per_day ORDER BY date DESC LIMIT 20)
)
"""


# ============================================================
# 视图 3: 每日市场宽度 (涨/跌/平 + 涨停/跌停 + 成交额)
# ============================================================
SQL_MARKET_BREADTH = r"""
CREATE OR REPLACE TABLE v_market_breadth AS
WITH clean AS (
    SELECT date,
        -- 与 v_factor_scores_daily 同口径: NaN/Inf 规范化为 NULL, 防 AVG 污染 (2026-09-02)
        CASE WHEN change_pct IS NULL OR ISNAN(change_pct) OR ISINF(change_pct)
             THEN NULL ELSE change_pct END AS change_pct,
        amount
    FROM daily_bars
    WHERE date >= (SELECT MAX(date) - 60 FROM daily_bars)
)
SELECT
    date,
    COUNT(*) AS total,
    SUM(CASE WHEN change_pct > 0 THEN 1 ELSE 0 END) AS n_up,
    SUM(CASE WHEN change_pct < 0 THEN 1 ELSE 0 END) AS n_down,
    SUM(CASE WHEN change_pct = 0 THEN 1 ELSE 0 END) AS n_flat,
    SUM(CASE WHEN change_pct >= 9.5 THEN 1 ELSE 0 END) AS n_limit_up,
    SUM(CASE WHEN change_pct <= -9.5 THEN 1 ELSE 0 END) AS n_limit_dn,
    SUM(amount) AS total_amount,
    AVG(change_pct) AS avg_change_pct,
    -- 宽度比: (n_up - n_down) / total
    (SUM(CASE WHEN change_pct > 0 THEN 1 ELSE 0 END) -
     SUM(CASE WHEN change_pct < 0 THEN 1 ELSE 0 END)) * 1.0 /
     NULLIF(COUNT(*), 0) AS breadth_ratio
FROM clean
GROUP BY date
ORDER BY date
"""


# ============================================================
# 视图 4: 北向 + 主力资金按日 TopN
# ============================================================
SQL_MONEY_FLOW_RANK = r"""
CREATE OR REPLACE TABLE v_money_flow_rank AS
WITH latest_day AS (
    SELECT MAX(fetch_time) AS d FROM money_flow_estimate
    WHERE fetch_time IS NOT NULL AND TRIM(fetch_time) <> ''
)
SELECT
    mf.fetch_time AS date,
    mf."行业" AS sector,
    COALESCE(mf."流入资金", 0) AS inflow,
    COALESCE(mf."流出资金", 0) AS outflow,
    COALESCE(mf."净额", 0) AS net_amount,
    COALESCE(mf."公司家数", 0) AS company_count,
    mf."领涨股" AS leader_stock,
    COALESCE(mf."领涨股-涨跌幅", 0) AS leader_change_pct,
    ROW_NUMBER() OVER (
        PARTITION BY mf.fetch_time
        ORDER BY COALESCE(mf."净额", 0) DESC
    ) AS rank_net
FROM money_flow_estimate mf
WHERE mf.fetch_time = (SELECT d FROM latest_day)
"""


# ============================================================
# 视图 5: 最新一日全 A 技术信号
# ============================================================
SQL_SIGNAL_SUMMARY = r"""
CREATE OR REPLACE TABLE v_signal_summary AS
WITH latest AS (SELECT MAX(date) AS d FROM daily_bars)
SELECT
    b.symbol AS canon,
    b.date,
    b.close, b.change_pct, b.turnover,
    -- 综合方向: 趋势 + 因子总分
    CASE
        WHEN v.f_trend > 0 AND v.f_signal > 0 THEN 'BUY'
        WHEN v.f_trend < 0 AND v.f_signal < 0 THEN 'SELL'
        ELSE 'HOLD'
    END AS signal,
    -- 6 因子综合分 (与 selector SCORE_WEIGHTS 一致, 此处仅归一化使用)
    (v.f_signal * 0.34 + v.f_trend * 0.14 + v.f_govern * 0.16 +
     v.f_liquidity * 0.08 + v.f_vol * 0.13 + v.f_mom_rev * 0.15) AS composite_score
FROM daily_bars b
JOIN v_factor_scores_daily v ON b.symbol = v.canon AND b.date = v.date
WHERE b.date = (SELECT d FROM latest)
  AND """ + A_SHARE_CODE_FILTER.replace("symbol", "b.symbol") + """
"""


# ============================================================
# 视图 6: 全 A 候选池快照
# ============================================================
SQL_UNIVERSE_SNAPSHOT = r"""
CREATE OR REPLACE TABLE v_universe_snapshot AS
WITH latest AS (SELECT MAX(date) AS d FROM daily_bars)
SELECT
    b.symbol AS canon,
    b.date,
    b.close, b.change_pct, b.turnover, b.amount,
    -- 简易过滤标记 (与 config.POOL_FILTER 对齐: turnover>=0.01 + 价非空)
    CASE WHEN b.turnover >= 0.01 AND b.change_pct IS NOT NULL THEN 1 ELSE 0 END AS pass_basic,
    si.signal,
    si.composite_score,
    ROW_NUMBER() OVER (ORDER BY si.composite_score DESC) AS rank_in_universe
FROM daily_bars b
JOIN v_signal_summary si ON b.symbol = si.canon
WHERE b.date = (SELECT d FROM latest)
  AND """ + A_SHARE_CODE_FILTER.replace("symbol", "b.symbol") + """
"""


# ============================================================
# [2026-09-05 迁移 m4] 物化视图调度: 纯 parquet 输出
# 不再向 DuckDB 写 CREATE TABLE; 所有视图由 h5i daily_bars (ts -> CAST(ts AS DATE))
# 计算, 输出同名 parquet 到 data/h5i/views/ (新规范) 并兼容写回
# data/views/parquet/ (dashboard._read_view_parquet 原路径).
# v_money_flow_rank 依赖 money_flow_estimate (m4 已迁入 h5i.northbound_money/
# money_flow_estimate), 优先读 h5i; DuckDB 仍在时只读兜底, 均缺失返回空结构.
# ============================================================

# A 股代码段 (与 A_SHARE_CODE_FILTER 等价的 Python 版)
_A_SHARE_CODES = ["000", "001", "002", "003", "300", "301", "302",
                  "600", "601", "603", "605", "688", "689"]
_IC_FACTORS = ["signal", "trend", "govern", "liquidity", "vol", "mom_rev"]


def _h5i_db():
    import h5i_db
    return h5i_db.Database(H5I_MARKET_DB)


def _max_bar_date(db) -> pd.Timestamp | None:
    df = db.sql("SELECT MAX(CAST(ts AS DATE)) m FROM daily_bars").to_pandas()
    if df is None or df.empty or df.iloc[0, 0] is None:
        return None
    return pd.Timestamp(df.iloc[0, 0])


def _clean_num(s: pd.Series) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce")
    return s.mask(np.isinf(s))


def _fetch_window(db, lookback_days: int, a_share_only: bool = True) -> pd.DataFrame:
    """读 h5i daily_bars 最近 lookback_days 自然日窗口.

    a_share_only=True: 仅保留 A 股代码段 (因子视图口径);
    False: 保留全市场行 (v_market_breadth 口径, 与 duck 版一致).
    """
    m = _max_bar_date(db)
    if m is None:
        return pd.DataFrame()
    lo = m - pd.Timedelta(days=lookback_days)
    df = db.sql(
        "SELECT symbol, CAST(ts AS DATE) AS date, open, high, low, close, volume, "
        "amount, change_pct, turnover FROM daily_bars "
        f"WHERE CAST(ts AS DATE) >= DATE '{lo:%Y-%m-%d}'"
    ).to_pandas()
    if df is None or df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    if a_share_only:
        df = df[df["symbol"].astype(str).str[:3].isin(_A_SHARE_CODES)]
    return df.sort_values(["symbol", "date"]).reset_index(drop=True)


def _write_view_parquet(name: str, df: pd.DataFrame) -> dict:
    """写 parquet 到 data/h5i/views + 兼容旧 data/views/parquet. 返回统计."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    if df is None or df.empty:
        table = pa.Table.from_pylist([])  # 空表; 无列信息时仅占位
        rows = 0
    else:
        table = pa.Table.from_pandas(df, preserve_index=False)
        rows = len(df)
        # 把 date 日期列规范为 date32 (与 duck 导出的物理类型一致, 便于下游 read_parquet)
        for c in list(table.column_names):
            if c in ("date",) and table.schema.field(table.schema.get_field_index(c)).type != pa.date32():
                try:
                    arr = pa.compute.cast(table[c], pa.date32())
                    table = table.set_column(table.schema.get_field_index(c), c, arr)
                except Exception:
                    pass
    paths = []
    for d in (H5I_VIEWS_DIR, PARQUET_DIR):
        os.makedirs(d, exist_ok=True)
        fp = os.path.join(d, f"{name}.parquet")
        pq.write_table(table, fp, compression="snappy")
        paths.append(fp)
    return {"path": paths[-1], "paths": paths, "rows": int(rows),
            "bytes": int(os.path.getsize(paths[0]))}


def _compute_factor_scores(db) -> pd.DataFrame:
    """视图 v_factor_scores_daily: 6 因子日分 (口径与 duck 版 SQL 一致)."""
    raw = _fetch_window(db, 120, a_share_only=True)
    cols = ["canon", "date", "f_signal", "f_trend", "f_govern",
            "f_liquidity", "f_vol", "f_mom_rev"]
    if raw is None or raw.empty:
        return pd.DataFrame(columns=cols)
    for c in ("amount", "change_pct", "turnover"):
        raw[c] = _clean_num(raw[c])

    g = raw.groupby("symbol", sort=False)

    def rmean(col: str, w: int) -> pd.Series:
        return g[col].rolling(w, min_periods=1).mean().reset_index(level=0, drop=True)

    raw["mom_20"] = rmean("change_pct", 20)
    tmp = raw.assign(_x2=(raw["change_pct"] ** 2))
    g2 = tmp.groupby("symbol", sort=False)["_x2"]
    raw["vol_20"] = np.sqrt(np.maximum(
        g2.rolling(20, min_periods=1).mean().reset_index(level=0, drop=True)
        - raw["mom_20"] ** 2, 0.0))
    raw["sma_20"] = rmean("close", 20)
    raw["sma_60"] = rmean("close", 60)

    up = ((raw["close"] > raw["sma_20"]) & (raw["close"] > raw["sma_60"])).astype(float)
    dn = ((raw["close"] < raw["sma_20"]) & (raw["close"] < raw["sma_60"])).astype(float)
    raw["f_signal"] = (up - dn) * raw["turnover"].fillna(0.0)
    raw["f_trend"] = np.select(
        [(raw["close"] > raw["sma_20"]) & (raw["sma_20"] > raw["sma_60"]),
         (raw["close"] < raw["sma_20"]) & (raw["sma_20"] < raw["sma_60"])],
        [1.0, -1.0], default=0.0)
    raw["f_govern"] = np.clip(1.0 - raw["change_pct"].abs().fillna(0.0) / 10.0, -1.0, 1.0)
    raw["f_liquidity"] = raw["turnover"].fillna(0.0)
    vol0 = raw["vol_20"].isna() | (raw["vol_20"] == 0) | np.isinf(raw["vol_20"].fillna(0.0)).values
    raw["f_vol"] = np.where(vol0, 0.0, 1.0 - np.minimum(1.0, raw["vol_20"] / 5.0))
    raw["f_mom_rev"] = np.where(raw["mom_20"].notna(), -raw["mom_20"] / 5.0, 0.0)

    out = raw[["symbol", "date", "f_signal", "f_trend", "f_govern",
               "f_liquidity", "f_vol", "f_mom_rev"]].rename(columns={"symbol": "canon"})
    return out[cols].sort_values(["canon", "date"]).reset_index(drop=True)


def _day_ic(x, y) -> float:
    """单日单因子 IC: cov/(std_x*std_y), 分母为 0 返回 NaN (DuckDB NULLIF 语义)."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    m = ~(np.isnan(x) | np.isnan(y))
    x, y = x[m], y[m]
    if x.size == 0:
        return float("nan")
    mx, my = float(x.mean()), float(y.mean())
    cov = float((x * y).mean()) - mx * my
    vx = max(float((x * x).mean()) - mx * mx, 0.0)
    vy = max(float((y * y).mean()) - my * my, 0.0)
    denom = np.sqrt(vx) * np.sqrt(vy)
    return cov / denom if denom > 0 else float("nan")


def _compute_ic_latest(db, scores: pd.DataFrame | None = None) -> pd.DataFrame:
    """视图 v_factor_ic_latest: 各因子最近 20 日 IC/ICIR/胜率."""
    cols = ["factor", "ic_value", "mean_20", "std_20", "icir_20", "win_rate_20", "n_days"]
    if scores is None or scores.empty:
        scores = _compute_factor_scores(db)
    if scores is None or scores.empty:
        return pd.DataFrame(columns=cols)
    ret = _fetch_window(db, 60, a_share_only=True)
    if ret is None or ret.empty:
        return pd.DataFrame(columns=cols)
    ret = ret.sort_values(["symbol", "date"])
    ret["nxt_close"] = ret.groupby("symbol")["close"].shift(-1)
    ret["fwd_1d"] = (ret["nxt_close"] / ret["close"] - 1.0)
    ret["fwd_1d"] = np.where(
        (ret["close"] > 0) & (ret["nxt_close"] > 0), ret["fwd_1d"], np.nan)
    joined = scores.merge(
        ret[["symbol", "date", "fwd_1d"]], left_on=["canon", "date"],
        right_on=["symbol", "date"], how="inner")
    joined = joined.drop(columns=["symbol"])
    joined = joined[joined["fwd_1d"].notna()]
    if joined.empty:
        return pd.DataFrame(columns=cols)
    # 逐日逐因子 IC
    rows = []
    for day, grp in joined.groupby("date"):
        ic_row = {"date": day}
        for f in _IC_FACTORS:
            ic_row[f] = _day_ic(grp["f_" + f].to_numpy(), grp["fwd_1d"].to_numpy())
        rows.append(ic_row)
    icd = pd.DataFrame(rows).sort_values("date", ascending=False).head(20).reset_index(drop=True)
    if icd.empty:
        return pd.DataFrame(columns=cols)

    def _nanmean(a):
        a = a[~np.isnan(a)]
        return float(a.mean()) if a.size else np.nan

    out = []
    for f in _IC_FACTORS:
        v = icd[f].to_numpy(dtype=float)
        n = int(len(v))
        mean = _nanmean(v)
        sq = _nanmean(v * v)
        std = np.sqrt(max(sq - (mean ** 2 if mean == mean else 0.0), 0.0)) if mean == mean else 0.0
        ic_value = 0.0 if mean != mean else mean
        icir = (mean / std) if (std and std > 0 and mean == mean) else np.nan
        win = float(np.sum(v[v == v] > 0)) / n if n else np.nan
        out.append({
            "factor": f,
            "ic_value": float(ic_value),
            "mean_20": float(ic_value),
            "std_20": float(std),
            "icir_20": (None if icir != icir else float(icir)),
            "win_rate_20": float(win),
            "n_days": int(n),
        })
    return pd.DataFrame(out, columns=cols)


def _compute_market_breadth(db) -> pd.DataFrame:
    """视图 v_market_breadth: 每日涨/跌/平 + 涨停/跌停 + 成交额 (全市场口径)."""
    cols = ["date", "total", "n_up", "n_down", "n_flat", "n_limit_up",
            "n_limit_dn", "total_amount", "avg_change_pct", "breadth_ratio"]
    raw = _fetch_window(db, 60, a_share_only=False)
    if raw is None or raw.empty:
        return pd.DataFrame(columns=cols)
    raw["chg"] = _clean_num(raw["change_pct"])
    by = raw.groupby("date")

    def _cnt(mask):
        s = raw.loc[mask].groupby("date").size()
        return s.reindex(by.size().index).fillna(0).astype(float)

    total = by.size().astype(float)
    n_up = _cnt(raw["chg"] > 0)
    n_down = _cnt(raw["chg"] < 0)
    n_flat = _cnt(raw["chg"] == 0)
    n_limit_up = _cnt(raw["chg"] >= 9.5)
    n_limit_dn = _cnt(raw["chg"] <= -9.5)
    total_amount = by["amount"].sum().fillna(0.0)
    avg_chg = by["chg"].mean()  # AVG 跳过 NULL (NaN), 与 duck 一致
    breadth = (n_up - n_down) * 1.0 / total.replace(0, np.nan)
    out = pd.DataFrame({
        "date": total.index, "total": total.values, "n_up": n_up.values,
        "n_down": n_down.values, "n_flat": n_flat.values,
        "n_limit_up": n_limit_up.values, "n_limit_dn": n_limit_dn.values,
        "total_amount": total_amount.values, "avg_change_pct": avg_chg.values,
        "breadth_ratio": breadth.values,
    })
    return out[cols].sort_values("date").reset_index(drop=True)


def _compute_money_flow_rank(db=None) -> pd.DataFrame:
    """视图 v_money_flow_rank: 优先读 h5i.money_flow_estimate (m4 已迁入,
    ts 时间列, 列转英文 sector/inflow/net_amount/...), 输出列序与 duck 版一致;
    DuckDB 仍在时只读兜底; 均缺失返回空结构 (不阻断其余视图)."""
    cols = ["date", "sector", "inflow", "outflow", "net_amount",
            "company_count", "leader_stock", "leader_change_pct", "rank_net"]

    # ---- 1) h5i 主源 ----
    if db is not None:
        try:
            df = db.sql("""
                SELECT CAST(ts AS VARCHAR) AS date,
                       sector,
                       COALESCE(inflow, 0) AS inflow,
                       COALESCE(outflow, 0) AS outflow,
                       COALESCE(net_amount, 0) AS net_amount,
                       COALESCE(company_count, 0) AS company_count,
                       leader_stock,
                       COALESCE(leader_change_pct, 0) AS leader_change_pct,
                       ROW_NUMBER() OVER (
                           PARTITION BY ts
                           ORDER BY COALESCE(net_amount, 0) DESC
                       ) AS rank_net
                FROM money_flow_estimate
                WHERE ts = (SELECT MAX(ts) FROM money_flow_estimate)
            """).to_pandas()
            if df is not None and not df.empty:
                df["date"] = df["date"].astype(str)
                return df[cols]
        except Exception:
            pass
    # ---- 2) DuckDB 兜底 (仍在时) ----
    if os.path.exists(DUCKDB_PATH):
        try:
            import duckdb
            con = duckdb.connect(DUCKDB_PATH, read_only=True)
            try:
                df = con.execute("""
                    WITH latest_day AS (
                        SELECT MAX(fetch_time) AS d FROM money_flow_estimate
                        WHERE fetch_time IS NOT NULL AND TRIM(fetch_time) <> ''
                    )
                    SELECT mf.fetch_time AS date, mf."行业" AS sector,
                           COALESCE(mf."流入资金", 0) AS inflow,
                           COALESCE(mf."流出资金", 0) AS outflow,
                           COALESCE(mf."净额", 0) AS net_amount,
                           COALESCE(mf."公司家数", 0) AS company_count,
                           mf."领涨股" AS leader_stock,
                           COALESCE(mf."领涨股-涨跌幅", 0) AS leader_change_pct,
                           ROW_NUMBER() OVER (
                               PARTITION BY mf.fetch_time
                               ORDER BY COALESCE(mf."净额", 0) DESC
                           ) AS rank_net
                    FROM money_flow_estimate mf
                    WHERE mf.fetch_time = (SELECT d FROM latest_day)
                """).df()
            finally:
                try:
                    con.close()
                except Exception:
                    pass
            if df is not None and not df.empty:
                df["date"] = df["date"].astype(str)
                return df[cols]
        except Exception:
            pass
    return pd.DataFrame(columns=cols)


def _compute_signal_summary(db, scores: pd.DataFrame | None = None) -> pd.DataFrame:
    """视图 v_signal_summary: 最新一日全 A 技术信号汇总."""
    cols = ["canon", "date", "close", "change_pct", "turnover",
            "signal", "composite_score"]
    if scores is None or scores.empty:
        scores = _compute_factor_scores(db)
    if scores is None or scores.empty:
        return pd.DataFrame(columns=cols)
    latest = scores["date"].max()
    s = scores[scores["date"] == latest].copy()
    bars = _fetch_window(db, 2, a_share_only=True)
    if bars is None or bars.empty:
        return pd.DataFrame(columns=cols)
    bars = bars[bars["date"] == latest][["symbol", "date", "close", "change_pct", "turnover"]]
    m = s.merge(bars, left_on=["canon"], right_on=["symbol"], how="inner")
    m = m.drop(columns=["symbol", "date_y"]).rename(columns={"date_x": "date"})
    if m.empty:
        return pd.DataFrame(columns=cols)
    comp = (m["f_signal"] * 0.34 + m["f_trend"] * 0.14 + m["f_govern"] * 0.16
            + m["f_liquidity"] * 0.08 + m["f_vol"] * 0.13 + m["f_mom_rev"] * 0.15)
    sig = np.select(
        [(m["f_trend"] > 0) & (m["f_signal"] > 0),
         (m["f_trend"] < 0) & (m["f_signal"] < 0)],
        ["BUY", "SELL"], default="HOLD")
    out = pd.DataFrame({
        "canon": m["canon"], "date": m["date"], "close": m["close"],
        "change_pct": m["change_pct"], "turnover": m["turnover"],
        "signal": sig, "composite_score": comp,
    })
    return out[cols].sort_values(["canon"]).reset_index(drop=True)


def _compute_universe_snapshot(db, sig: pd.DataFrame | None = None) -> pd.DataFrame:
    """视图 v_universe_snapshot: 最新一日全 A 候选池快照."""
    cols = ["canon", "date", "close", "change_pct", "turnover", "amount",
            "pass_basic", "signal", "composite_score", "rank_in_universe"]
    if sig is None or sig.empty:
        sig = _compute_signal_summary(db)
    if sig is None or sig.empty:
        return pd.DataFrame(columns=cols)
    latest = sig["date"].max()
    bars = _fetch_window(db, 2, a_share_only=True)
    if bars is None or bars.empty:
        return pd.DataFrame(columns=cols)
    bars = bars[bars["date"] == latest][["symbol", "amount"]]
    m = sig.merge(bars, left_on="canon", right_on="symbol", how="left").drop(columns=["symbol"])
    if m.empty:
        return pd.DataFrame(columns=cols)
    m["pass_basic"] = ((m["turnover"] >= 0.01) & (m["change_pct"].notna())).astype(int)
    order = m["composite_score"].fillna(-np.inf).values
    rank = np.argsort(-order, kind="stable") + 1
    m["rank_in_universe"] = rank
    return m[cols].sort_values("rank_in_universe").reset_index(drop=True)


def build_views(read_only_db: bool = True, only: list[str] | None = None) -> dict:
    """物化所有视图并导出 Parquet (纯 parquet 输出, 不再写 DuckDB 物化表).

    read_only_db: 保留兼容参数 (不再打开 DuckDB 写连接; DuckDB 文件保持只读).
    only: 只构建指定子集, 用于增量.
    输出: data/h5i/views/<view>.parquet (新规范) + data/views/parquet/<view>.parquet (兼容).
    """
    os.makedirs(VIEWS_DIR, exist_ok=True)
    os.makedirs(H5I_VIEWS_DIR, exist_ok=True)
    only = only or [
        "v_factor_scores_daily", "v_factor_ic_latest",
        "v_market_breadth", "v_money_flow_rank",
        "v_signal_summary", "v_universe_snapshot",
    ]
    out = {"ok": True, "generated_at": _now(),
           "mode": "h5i-parquet", "views": {}, "meta_file": META_FILE}
    cache: dict = {}
    try:
        db = _h5i_db()
    except Exception as e:
        out["ok"] = False
        for name in only:
            out["views"][name] = {"ok": False, "error": f"h5i 打开失败: {e}"}
        try:
            with open(META_FILE, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return out

    try:
        for name in only:
            t0 = time.time()
            try:
                if name == "v_factor_scores_daily":
                    df = _compute_factor_scores(db)
                elif name == "v_factor_ic_latest":
                    cache.setdefault("scores", _compute_factor_scores(db))
                    df = _compute_ic_latest(db, cache["scores"])
                elif name == "v_market_breadth":
                    df = _compute_market_breadth(db)
                elif name == "v_money_flow_rank":
                    df = _compute_money_flow_rank(db)
                elif name == "v_signal_summary":
                    cache.setdefault("scores", _compute_factor_scores(db))
                    df = _compute_signal_summary(db, cache["scores"])
                elif name == "v_universe_snapshot":
                    cache.setdefault("signal",
                                     _compute_signal_summary(db, cache.get("scores")))
                    df = _compute_universe_snapshot(db, cache["signal"])
                else:
                    out["views"][name] = {"ok": False, "error": "unknown view"}
                    continue
                pq = _write_view_parquet(name, df)
                out["views"][name] = {
                    "ok": True,
                    "rows": pq["rows"],
                    "bytes": pq["bytes"],
                    "parquet": pq["path"],
                    "paths": pq["paths"],
                    "ms": int((time.time() - t0) * 1000),
                }
            except Exception as e:
                out["views"][name] = {
                    "ok": False,
                    "error": f"{type(e).__name__}: {e}",
                    "ms": int((time.time() - t0) * 1000),
                }
                out["ok"] = False
        with open(META_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    finally:
        try:
            db.close()
        except Exception:
            pass
    return out


def read_view_summary() -> dict:
    """读取 build_meta.json 给 dashboard 用."""
    if not os.path.exists(META_FILE):
        return {"ok": False, "error": "build_meta.json 不存在"}
    try:
        with open(META_FILE, encoding="utf-8") as f:
            j = json.load(f)
        return {"ok": True, "meta": j}
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--readonly", action="store_true",
                    help="兼容参数 (纯 parquet 输出, 不再写 DuckDB)")
    args = ap.parse_args()
    r = build_views(read_only_db=args.readonly)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    sys.exit(0 if r["ok"] else 1)
