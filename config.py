# ============================================================
# config.py -- 全A轮动模拟盘 配置文件
# 方案: 日频选股(DuckDB全A) + 盘中纸面撮合(AKShare), 只做多, T+1, 全额资金
# ============================================================

import os

# ---- 工作区 / 输出 ----
BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
DAILY_DIR = os.path.join(DATA_DIR, "daily")       # 每日回执 data/daily/<date>/
SNAP_DIR = os.path.join(DATA_DIR, "snapshots")    # 盘中快照
STATE_FILE = os.path.join(DATA_DIR, "state.json") # 运行状态(持仓/净值/回执索引)

# ---- 本地数据源 ----
# [2026-09-05 m4] DuckDB 已退役删除: 全A日频行情已迁入 h5i
#   data/h5i/market.db (daily_bars/financials/valuation/valuation_snapshot/
#   northbound_money/money_flow_estimate) + data/h5i/static/symbols.parquet.
# 下方 DUCKDB_PATH 常量仅为历史引用兼容保留(不再指向任何存在文件); 读取方
# 均已改为优先 h5i 并在缺失时降级. 设 BAR_STORE=duck 仅作对照(文件已不存在,
# 会被 duck_available() 判 False).
DUCKDB_PATH = os.path.join(BASE, "data", "legacy_stockdb.duckdb")  # [已退役] 原全A日频数据湖, 本机已删除

# ---- 数据源优先级 ----
# 主数据源: h5i(全A日频, m4 起), 盘中/缺失校验: AKShare.
# 下列标签仅为历史兼容保留, 实际取数由 BAR_STORE(默认 h5i) 与 duck_available() 决定.
PRIMARY_SOURCE = "duckdb"      # 历史标签, 不再参与取数路由
FALLBACK_SOURCE = "akshare"

# ---- 资金与交易制度(A股: 只做多 / T+1 / 全额资金) ----
INIT_CAPITAL = 100_000.0      # 起始虚拟资金 10万
MAX_POS_RATIO = 0.95            # 单账户最大仓位比例(留5%现金)
MAX_STOCKS = 10                 # 全A轮动持有股票数 Top N

# ---- 目标权重分配 (2026-09-05 策略层优化: 关闭 target_weight 悬空契约) ----
# 历史: DRL plan 每项带 target_weight(等权 1/n) 但 realtime/backtest 引擎始终按
#       等权 band=total_target_mv/n 撮合 -> premarket 健康检查长期报「契约悬空」.
# 现在: 引擎消费每项 target_weight; 权重由分配器生成:
#   mode="fml": 用 f_ml(lambdarank, 重映射后收益尺度) 做 rank 线性收缩加权,
#               预期收益更高的标的获得更大目标权重(高置信暴露), 再叠加集中度上限.
#   mode="equal": 等权 1/n (回退/对照).
# 契约: 权重和为 1.0 (相对总资产), 与 premarket_healthcheck 校验一致.
TARGET_WEIGHT = {
    "mode": "fml",             # "fml" | "equal"
    "min_mult": 0.60,          # 权重下限 = min_mult * equal (低确信票保底)
    "max_mult": 1.15,          # 权重上限(集中度封顶) = max_mult * equal;
                               # n=10 -> [6.0%, 11.5%] 相对总资产; 对应市值/权益
                               # <=~11%, 低于集中度触发线 12.4%, 留 ~1.4pp 安全带
}

# ---- 因子融合打分 (方案A: 四因子 ICIR 加权, 替换 f_ml; 见 factor_fusion.py) ----
# FUSION_SCORE 默认=1(开); =0 关闭融合走旧 f_ml.
# FORCE_FML=1 强制走旧 ml_fusion_bridge.compute_fml(调试/对照用).
# 两开关仅作配置缺省快照, 运行时可被环境变量实时覆盖.
FUSION_SCORE = os.environ.get("FUSION_SCORE", "1") != "0"
FORCE_FML = os.environ.get("FORCE_FML", "0") == "1"

# ---- 候选池过滤规则 ----
POOL_FILTER = {
    "min_float_market_cap": 50e8,     # 最小流通市值 50亿(剔除微小盘流动性差)
    "max_float_market_cap": 2000e8,   # 最大流通市值 2000亿(剔除超大盘)
    "max_st_price": 999999,           # 排除ST/*ST(价>阈值即视为非ST, 需名称过滤,见selector)
    "min_avg_turnover": 0.01,         # 20日均换手率下限
    "exclude_st": True,               # 排除ST/*ST
    "exclude_new": 120,               # 排除上市不足120自然日(次新股)
    "board_allow_all": True,          # 沪深主板+创业板+科创板
    "exclude_920_bse": True,          # 排除920段(北交所代码段, 数据源偶发误标sh, 防御性剔除)
}

# ---- P08 信号参数(与回测版 V4 一致) ----
SIGNAL_PARAMS = {
    "atr_period": 14,
    "rsi_period": 14,
    "signal_threshold": 0.3,
    "sma_short": 20,
    "sma_mid": 60,
    "sma_long": 120,
}

# ---- CVaR-PPO 参数 (2026-09-06 升级: 尾部风险嵌入优化目标) ----
# 在标准 PPO loss 中增加 CVaR 约束项: total_loss = ppo_loss + cvar_coef × cvar_loss
# cvar_alpha: 尾部百分位 (默认 0.05 = 95% CVaR, 取最差 5% 轨迹回报)
# cvar_coef:  CVaR 约束项权重 (默认 0.1, 与 FinAI Contest 2025 UG-CPPO 相当)
# 优先级: CLI 参数 > 环境变量 > 本配置 > CVaR_PPO 类默认值
CVAR_PPO = {
    "cvar_alpha": float(os.environ.get("CVAR_ALPHA", "0.05")),
    "cvar_coef": float(os.environ.get("CVAR_COEF", "0.1")),
}

# ---- DRL 超参数自适应 (2026-09-06 升级) ----
# 监控训练过程中的策略熵值, 当熵值低于阈值时自动:
#   - 增加 ent_coef (鼓励探索, 乘以 ent_coef_boost)
#   - 降低学习率 (稳定更新, 乘以 lr_decay_factor)
# entropy_threshold = -1 时关闭自适应 (默认)
# 参考: UG-CPPO 的"不确定性门控"机制
DRL_ADAPT = {
    "entropy_threshold": float(os.environ.get("ENTROPY_THRESHOLD", "-1.0")),
    "lr_decay_factor": float(os.environ.get("LR_DECAY_FACTOR", "0.8")),
    "ent_coef_boost": float(os.environ.get("ENT_COEF_BOOST", "1.5")),
}

# ---- Risk-First 架构 (2026-09-07 升级: LLM 风险信号结构化约束层) ----
# 参考: 2026 FinRL-DeepSeek 研究发现, 标准 DRL 代理会忽略 LLM 的风险信号
# 配置项:
#   variance_filter_window: 方差过滤器窗口
#   variance_filter_n_std: 异常判据 (信号偏离 N 倍标准差)
#   exposure_penalty_coef: 暴露惩罚系数
#   circuit_breaker_drawdown: 熔断回撤阈值
#   circuit_breaker_vol: 熔断波动率阈值
RISK_FIRST = {
    "variance_filter_window": int(os.environ.get("RF_VAR_WINDOW", "20")),
    "variance_filter_n_std": float(os.environ.get("RF_VAR_NSTD", "2.5")),
    "exposure_penalty_coef": float(os.environ.get("RF_EXPOSURE_COEF", "0.5")),
    "circuit_breaker_drawdown": float(os.environ.get("RF_CB_DRAWDOWN", "0.08")),
    "circuit_breaker_vol": float(os.environ.get("RF_CB_VOL", "0.35")),
    "circuit_breaker_cvar": float(os.environ.get("RF_CB_CVAR", "0.05")),
    "risk_first_enabled": os.environ.get("RISK_FIRST_ENABLED", "1") != "0",
}

# ---- Logic-Q 神经符号化趋势分析 (2026-09-07 升级) ----
# 参考: 2026 Logic-Q 框架, 通过符号化趋势规则动态调整策略网络参数
# 配置项:
#   ma_cross_threshold: 均线交叉阈值
#   sr_breakout_threshold: 支撑阻力突破阈值
#   volume_confirmation: 成交量确认权重
LOGIC_Q = {
    "ma_cross_threshold": float(os.environ.get("LQ_MA_CROSS", "0.02")),
    "sr_breakout_threshold": float(os.environ.get("LQ_SR_BREAK", "0.015")),
    "volume_confirmation": float(os.environ.get("LQ_VOL_CONF", "0.5")),
    "logic_q_enabled": os.environ.get("LOGIC_Q_ENABLED", "1") != "0",
}

# ---- PPO 动态因子权重优化 (2026-09-06 升级) ----
# 模式: "ic_weight" (IC 序列 → 权重增量, 原有) |
#       "factor_value" (因子值 → 权重, 动态复权)
# 参考: DTLC_RL 框架的特征空间解耦
DRL_FACTOR_OPT = {
    "mode": os.environ.get("DRL_FACTOR_MODE", "ic_weight"),
    "factor_lookback": int(os.environ.get("DRL_FACTOR_LOOKBACK", "5")),
}

# ---- 多尺度信号分解 + Hybrid-GRPO (2026-09-07 升级) ----
# 参考: 2026 年框架用小波分解分离趋势和波动, 再结合组相对策略优化
WAVELET = {
    "level": int(os.environ.get("WAVELET_LEVEL", "3")),
    "window": int(os.environ.get("WAVELET_WINDOW", "60")),
    "grpo_group_size": int(os.environ.get("GRPO_GROUP_SIZE", "4")),
    "grpo_coef": float(os.environ.get("GRPO_COEF", "0.3")),
    "wavelet_enabled": os.environ.get("WAVELET_ENABLED", "1") != "0",
}

# ---- Hi-DARTS 层次化多智能体 (2026-09-07 升级) ----
# 参考: 2025 Hi-DARTS 框架, 元智能体分析市场波动, 动态激活子智能体
HIDARTS = {
    "vol_threshold_low": float(os.environ.get("HIDARTS_VOL_LOW", "0.15")),
    "vol_threshold_high": float(os.environ.get("HIDARTS_VOL_HIGH", "0.30")),
    "trend_threshold": float(os.environ.get("HIDARTS_TREND_THRESH", "0.02")),
    "hidarts_enabled": os.environ.get("HIDARTS_ENABLED", "1") != "0",
}

# ---- StockMARL 多智能体模拟 (2026-09-07 升级) ----
# 参考: 2025 StockMARL, 让 RL 观察模拟投资者行为学习
STOCKMARL = {
    "n_agents": int(os.environ.get("SMARL_N_AGENTS", "4")),
    "marl_signal_dim": int(os.environ.get("SMARL_SIGNAL_DIM", "4")),
    "marl_enabled": os.environ.get("SMARL_ENABLED", "1") != "0",
}

# ---- 可解释 RL + 自适应特征选择 (2026-09-07 升级) ----
# 参考: 2025 A 股实证, 累计收益 88.80% 超越 DQN 基线 20.76%
EXPLAINABLE_RL = {
    "feature_ic_threshold": float(os.environ.get("XRL_IC_THRESH", "0.02")),
    "feature_eval_interval": int(os.environ.get("XRL_EVAL_INTERVAL", "20")),
    "min_features": int(os.environ.get("XRL_MIN_FEATURES", "3")),
    "xrl_enabled": os.environ.get("XRL_ENABLED", "1") != "0",
}

# ---- 风险因子 PPO 动态优化 (2026-09-07 升级) ----
# 参考: 2025 量化报告用 PPO 动态优化风险因子生成, 解释度提升至 35.3%,
#       因子时序更稳定. 把波动率/CVaR/最大回撤风险因子并入 PPO 观测.
RISK_FACTOR_PPO = {
    "risk_factor_window": int(os.environ.get("RFP_WINDOW", "20")),
    "cvar_alpha": float(os.environ.get("RFP_CVAR_ALPHA", "0.05")),
    "lookback": int(os.environ.get("RFP_LOOKBACK", "5")),
    "rfp_enabled": os.environ.get("RFP_ENABLED", "1") != "0",
}

# ---- CAFPO 条件自编码因子投资 (2026-09-07 升级, 长期方向) ----
# 参考: 2025 CAFPO 在 94 公司特征条件下压缩股票收益, 样本外 24.6% 复合收益
#       夏普 0.94. 用于从海量因子中提取潜在风险因子.
CAFPO = {
    "latent_dim": int(os.environ.get("CAFPO_LATENT_DIM", "4")),
    "steps": int(os.environ.get("CAFPO_STEPS", "400")),
    "lr": float(os.environ.get("CAFPO_LR", "0.01")),
    "holdout_frac": float(os.environ.get("CAFPO_HOLDOUT", "0.2")),
    "cafpo_enabled": os.environ.get("CAFPO_ENABLED", "1") != "0",
}

# ---- 执行层滑点分解 (2026-09-07 升级: 市场冲击 vs 执行风险) ----
# 参考: Almgren & Chriss (2001). 滑点 = 市场冲击(参与率×波动率) +
#       执行风险(σ×√(执行时长/252)×紧迫度), 让模拟盘更接近实盘.
EXECUTION = {
    "execution_horizon_days": float(os.environ.get("EXEC_HORIZON_DAYS", "0.0")),
    "urgency_kappa": float(os.environ.get("EXEC_URGENCY_KAPPA", "1.0")),
    "permanent_coef": float(os.environ.get("EXEC_PERM_COEF", "0.1")),
    "temporary_coef": float(os.environ.get("EXEC_TEMP_COEF", "0.5")),
    "max_slippage": float(os.environ.get("EXEC_MAX_SLIPPAGE", "0.02")),
    "execution_enabled": os.environ.get("EXECUTION_ENABLED", "1") != "0",
}

# ---- 轮动打分权重 ----
# 打分 = 信号分(34%) + 趋势分(14%) + 治理分(16%) + 流动性分(8%)
#       + alpha(28%): vol(10%) + pb_rev(6%) + roe(6%) + mf_net(6%)
SCORE_WEIGHTS = {
    "signal": 0.34,     # P08 技术信号(raw_signal, 已含RSI/MACD/SMA/突破)
    "trend": 0.14,      # 趋势强度(MA多头排列)
    "govern": 0.16,     # 治理/质量(ROE, 负债率, 盈利稳定, 无ST/诉讼占位)
    "liquidity": 0.08,  # 流动性(20日成交额)
    "vol": 0.10,        # 低波动 alpha
    "mom_rev": 0.0,     # 反转动量: 暂停 (短窗IC=-0.36, ICIR=-2.88, 深度失效)
    "pb_rev": 0.06,     # 低PB (深度价值, 待IC回算)
    "roe": 0.06,        # 高ROE (质量alpha, 待IC回算)
    "mf_net": 0.06,     # 资金净流入 (资金面, 待IC回算)
}

# ---- 下单通道 ----
# 唯一有效的下单通道开关. 当前系统为纯模拟盘 (PaperBook 纸面撮合),
# easytrader 等真实券商下单尚未接入, 也暂不使用.
# 值域: "paper"       模拟盘下单 (当前唯一生效, 成交仅记账, 不连券商)
#       "easytrader"  实盘下单通道 (预留; 接入前 keep "paper")
TRADE_BROKER = "paper"


# ---- 盘中纸面撮合 ----
PAPER = {
    "tplus1": True,          # T+1: 当日买入不可当日卖
    "commission": 0.00025,   # 佣金 万2.5
    "stamp_tax": 0.0005,     # 印花税 卖~单边0.05% (2023.8起减半)
    "transfer_fee": 0.00001, # 过户费 万0.1
    "slippage": 0.0005,      # 滑点 万分之5(计入成交价)
    "min_amount_sell": 100,  # 卖出最低100元
    # 回测与实盘统一成本模型: 滑点+冲击成本合计当作单边费率(双向计提), 与实盘撮合
    # (slippage计入成交价)对齐; vnpy 只支持按方向各一费率, 故此处把滑点近似进费率.
    "impact_cost": 0.0002,   # 冲击成本 万2 (成交额单边; 回测侧近似费率, 实盘侧近似滑点加成)
    # ---- 风控 (行为层, 撮合时强制拦截) ----
    "stop_loss": 0.03,       # 单票浮亏跌破 -3% 触发减仓止损
    "max_single_weight": 0.08,  # 单票市值占总权益上限 8% (防集中)
    # 集中度触发系数: 压回触发线 = max_single_weight * concentration_trigger_mult.
    # 2026-09-05 由硬编码 1.35(触发 10.8%)上调至 1.55(触发 12.4%):
    # 配合 fml 目标加权(单票上限 11.5%) 留出安全边际, 避免加权目标自我触发压回振荡.
    "concentration_trigger_mult": 1.55,
    "portfolio_drawdown": 0.08, # 组合级回撤达 -8% 熔断暂停加仓
    # ---- 降摩擦: 单日换手预算 (2026-09-02 接入) ----
    # 日频全量换仓的买卖费用是模拟盘主要失血点(10日约0.8%本金).
    # 这里限制单日"策略调仓"(卖出离池 + 买入补权) 的成交额合计不超过
    # 净资产 * max_turnover_pct; 超限则跳过本次调仓, 保留现金等后续落单.
    # 止损卖出 / 组合风控压回 属必要风控, 不占用此预算(始终执行).
    "max_turnover_pct": 0.20,  # 单日策略买卖成交额合计占净资产上限 20%
    # ---- 降摩擦: 小单门槛 (2026-09-02 接入) ----
    # 补权买入的名义额(成交额, 含买入滑点+冲击)低于该值不出手. 佣金有最低5元,
    # 小额补权(如几百元)的手续费占比畸高, 是额外摩擦失血点; 跳过小单等累积到
    # 足够价差/资金再补, 可显著降低无效交易次数.
    "min_reorder_notional": 1000.0,  # 单笔补权名义额下限(元); 低于此值不补权
    # ---- 降摩擦: 最小持仓天数 (2026-09-03 接入) ----
    # 持仓股至少持有 N 天才能被卖出(止损/风控压回除外).
    # 避免"今天买入明天卖出"的高频换手烧手续费.
    "min_hold_days": 2,  # 最小持仓天数; 低于此天数不卖出(风控除外)
    # ---- 降摩擦: 成本门槛三件套 (2026-09-04 接入) ----
    # 往返费率参考: 佣金万2.5 + 过户万0.1 + 滑点万5 + 冲击万2 (双边),
    # 卖出另计印花税万5 -> 一次完整买卖约 0.25% 本金, 这是日频全换仓的主要失血点.
    # 1) 调仓间隔 (方向2/降频): 策略调仓(离池卖出+补权买入)最小间隔自然日.
    #    期间仅执行止损/组合风控, 不做策略换仓 -> 从每日调仓降到约每周2次,
    #    手续费累积近似降为 1/3. 牺牲部分换仓收益换取成本占比下降.
    "rebalance_interval_days": 3,
    # 2) 单笔最小相对仓位 (方向3/摊薄固定成本): 补权名义额须 >=
    #    净资产*min_reorder_weight_pct(%), 与 min_reorder_notional 取较大者.
    #    单笔太小(如5%仓位的零头)时, 最低5元佣金+滑点占比畸高; 提高单笔
    #    名义额把单位交易成本摊薄到可接受区间.
    "min_reorder_weight_pct": 1.5,
    # 3) 最低信号方向 (方向1/预期收益覆盖成本): plan 目标票若明确带
    #    空头/离场标签(source_signal 非多), 其预期边际收益不足以覆盖往返
    #    手续费, 禁止买入. 强多头 source_signal=BUY 才允许开新仓.
    "min_signal_buy": True,
}

# ---- 每日调度时间 ----
SCHEDULE = {
    "daily_select": "15:05",   # 收盘后选股(生成次日目标)
    "intraday_bar": "02:00",   # 盘中定期价刷新间隔(测试用; 实盘可调到 9:35开始)
    "close_time": "15:00",     # 收盘
}

# ---- 数据库每日补录 ----
# 收盘后统一补录的全表头清单, 与 DuckDB 表名一一对应. None 代表不存在或不需要同步.
# 每行: 表名 -> (日期列名, 日期格式 "date"/"datetime", 同步函数名 in update_db)
DB_UPDATE_TARGETS = {
    "daily_bars":             ("date",                "date",    "sync_daily_bars"),
    "valuation_snapshot":     ("fetch_time",          "datetime","sync_valuation_snapshot"),
    "adj_factors":            ("trade_date",          "date",    "sync_adj_factors"),
    "northbound_money":       ("trade_date",          "date",    "sync_northbound_money"),
    "margin_daily":           ("trade_date",          "date",    "sync_margin_daily"),
    "dzjy_daily":             ("trade_date",          "date",    "sync_dzjy_daily"),
    "money_flow_estimate":    ("trade_date",          "date",    "sync_money_flow_estimate"),
    "events":                 ("trade_date",          "date",    "sync_events"),
    "lhb":                    ("trade_date",          "date",    "sync_lhb"),
    "lhb_detail":             (None,                  None,      "sync_lhb_detail"),
    "orderbook_snapshot":     ("snapshot_date",       "date",    "sync_orderbook_snapshot"),
    "block_trade":            ("trade_date",          "date",    "sync_block_trade"),
    "financials":             ("report_date",         "date",    "sync_financials"),
    "announcements":          (None,                  None,      "sync_announcements"),
    "news":                   (None,                  None,      "sync_news"),
    "stock_news":             (None,                  None,      "sync_stock_news"),
    "stock_notices":          ("notice_date",         "date",    "sync_stock_notices"),
    "news_cctv":              (None,                  None,      "sync_news_cctv"),
    "dividends":              (None,                  None,      "sync_dividends"),
    "repurchases":            (None,                  None,      "sync_repurchases"),
    "earnings_forecasts":     (None,                  None,      "sync_earnings_forecasts"),
    "economic_events":        ("event_time",          "datetime","sync_economic_events"),
    "share_structure":        (None,                  None,      "sync_share_structure"),
    "shareholder_changes":    (None,                  None,      "sync_shareholder_changes"),
    "corporate_actions":      (None,                  None,      "sync_corporate_actions"),
    "locked_shares":          (None,                  None,      "sync_locked_shares"),
    "restricted_releases":    (None,                  None,      "sync_restricted_releases"),
    "minute_bars":            ("trade_date",          "date",    "sync_minute_bars"),
}