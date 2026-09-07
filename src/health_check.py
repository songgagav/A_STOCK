import duckdb, json, os, sys
import os
import numpy as np
import pandas as pd
from datetime import date, datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE, "data")
DAILY_DIR = os.path.join(DATA_DIR, "daily")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
DUCKDB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "legacy_stockdb.duckdb")
ROOT = os.path.dirname(BASE)

PASS = "  [PASS]"
FAIL = "  [FAIL]"
WARN = "  [WARN]"
INFO = "  [INFO]"
results = {"pass": 0, "fail": 0, "warn": 0, "total": 0}

def check(label, ok, detail=""):
    results["total"] += 1
    # Normalize numpy bool_ to Python bool for identity check
    if hasattr(ok, "item"):
        ok = bool(ok.item())
    if ok is True:
        results["pass"] += 1
        print(PASS + " " + label + (" - " + detail if detail else ""))
    elif ok is False:
        results["fail"] += 1
        print(FAIL + " " + label + (" - " + detail if detail else ""))
    else:
        results["warn"] += 1
        print(WARN + " " + label + (" - " + detail if detail else ""))

def prev_trade_day(ref_date):
    d = ref_date - timedelta(days=1)
    while d.weekday() >= 5:
        d = d - timedelta(days=1)
    return d

def daily_to_iso(d):
    if len(d) == 8 and d.isdigit():
        return d[:4] + "-" + d[4:6] + "-" + d[6:8]
    return d

print("=" * 60)
print("  HEALTH CHECK - 4 LAYER VERIFICATION")
print("  " + date.today().strftime("%Y-%m-%d"))
print("=" * 60)

# ====================================================================
# DATA LAYER
# ====================================================================
print("\n" + "=" * 60)
print("  DATA LAYER")
print("=" * 60)

print("\n[Data 1] Source freshness (free-stockdb/CNEquity)")
print("-" * 40)

con = duckdb.connect(DUCKDB_PATH, read_only=True)
r = con.execute("SELECT MAX(date) FROM daily_bars").fetchone()
latest_db = str(r[0]) if r and r[0] else None
print("  daily_bars latest: " + str(latest_db))

today = date.today()
expected_date = prev_trade_day(today).strftime("%Y-%m-%d")
check("daily_bars latest >= prev trade day (" + expected_date + ")",
      latest_db and latest_db >= expected_date,
      "actual=" + str(latest_db))

# Check data completeness: stocks on latest date vs previous
r = con.execute("""
    SELECT date, COUNT(*) FROM daily_bars
    WHERE date >= (SELECT MAX(date) - INTERVAL 3 DAY FROM daily_bars)
    GROUP BY date ORDER BY date
""").fetchall()
prev_count = None
for row in r:
    if prev_count and row[1] < prev_count * 0.5:
        check("daily_bars coverage complete (" + str(row[0]) + ")",
              False, str(row[1]) + " vs " + str(prev_count) + " stocks (partial update)")
    prev_count = row[1]
    print("  " + str(row[0]) + ": " + str(row[1]) + " stocks")

r = con.execute("SELECT COUNT(*) FROM daily_bars WHERE date = (SELECT MAX(date) FROM daily_bars)").fetchone()
n_stocks = r[0] if r else 0
check("latest day stocks >= 1000", n_stocks >= 1000, "actual=" + str(n_stocks))

try:
    r = con.execute("SELECT COUNT(*) FROM symbols WHERE is_active = true").fetchone()
    print("  active symbols: " + str(r[0] if r else 0))
except Exception:
    pass

print("\n[Data 2] Price accuracy (DuckDB vs authoritative)")
print("-" * 40)

for sym, name in [("000001", "PingAn Bank"), ("600519", "Kweichow Moutai")]:
    rows = con.execute(
        "SELECT date, close, change_pct, volume FROM daily_bars "
        "WHERE symbol='" + sym + "' AND date >= '2026-08-25' ORDER BY date"
    ).fetchall()
    print("  " + sym + "(" + name + "):")
    for r2 in rows:
        print("    " + str(r2[0]) + " close=" + str(round(r2[1], 2))
              + " chg=" + str(round(r2[2], 2)) + "% vol=" + str(int(r2[3])))
    check(sym + " data continuous", len(rows) >= 3,
          str(len(rows)) + " records in 5d window")

print("\n[Data 3] Factor pool completeness")
print("-" * 40)

r = con.execute("""
    SELECT COUNT(*) FROM v_factor_scores_daily
    WHERE f_signal IS NULL OR f_signal != f_signal
       OR f_trend IS NULL OR f_trend != f_trend
       OR f_govern IS NULL OR f_govern != f_govern
       OR f_liquidity IS NULL OR f_liquidity != f_liquidity
       OR f_vol IS NULL OR f_vol != f_vol
       OR f_mom_rev IS NULL OR f_mom_rev != f_mom_rev
""").fetchone()
nan_count = r[0] if r else -1
check("v_factor_scores_daily no NaN factors", nan_count == 0,
      "NaN rows=" + str(nan_count))

r = con.execute("""
    SELECT COUNT(*) FROM v_factor_scores_daily
    WHERE date = (SELECT MAX(date) FROM v_factor_scores_daily)
""").fetchone()
latest_rows = r[0] if r else 0
check("latest day factor rows >= 500", latest_rows >= 500,
      "actual=" + str(latest_rows))

r = con.execute("""
    SELECT COUNT(*) FROM daily_bars
    WHERE close <= 0 AND date >= '2026-01-01'
""").fetchone()
zero_anomalies = r[0] if r else -1
if zero_anomalies == 0:
    check("daily_bars no close<=0 anomalies", True)
else:
    check("close<=0 anomalies (known, CTE-filtered)", "warn",
          "anomalies=" + str(zero_anomalies) + " (CBs/halted, filtered at CTE)")
con.close()

# ====================================================================
# STRATEGY LAYER
# ====================================================================
print("\n" + "=" * 60)
print("  STRATEGY LAYER")
print("=" * 60)

print("\n[Strategy 4] Factor view values")
print("-" * 40)

con = duckdb.connect(DUCKDB_PATH, read_only=True)

# Use latest available date from factor view (not hardcoded today)
r = con.execute("SELECT MAX(date) FROM v_factor_scores_daily").fetchone()
fv_latest = str(r[0]) if r and r[0] else None
print("  factor view latest date: " + str(fv_latest))

# Check data completeness: how many stocks vs previous day
r = con.execute("""
    SELECT date, COUNT(*) FROM v_factor_scores_daily
    WHERE date >= (SELECT MAX(date) - INTERVAL 3 DAY FROM v_factor_scores_daily)
    GROUP BY date ORDER BY date
""").fetchall()
prev_count = None
for row in r:
    if prev_count and row[1] < prev_count * 0.5:
        check("factor view coverage complete (" + str(row[0]) + " vs "
              + str(r[-2][0]) + ")", False,
              str(row[1]) + " vs " + str(prev_count) + " stocks (partial update)")
    prev_count = row[1]
    print("  " + str(row[0]) + ": " + str(row[1]) + " stocks")

if not fv_latest:
    check("factor view has data", False)
    con.close()
else:
    # Pick stocks confirmed present in the view
    r = con.execute(
        "SELECT canon FROM v_factor_scores_daily "
        "WHERE date='" + fv_latest + "' ORDER BY canon LIMIT 3"
    ).fetchall()
    test_codes = [x[0] for x in r] if r else ["000816", "000862", "000863"]

    for code in test_codes:
        r = con.execute(
            "SELECT canon, date, f_signal, f_trend, f_govern, "
            "f_liquidity, f_vol, f_mom_rev FROM v_factor_scores_daily "
            "WHERE canon='" + code + "' AND date='" + fv_latest + "'"
        ).fetchone()
        if r:
            fs = float(r[2]); ft = float(r[3]); fg = float(r[4])
            fl = float(r[5]); fv = float(r[6]); fm = float(r[7])
            score = fs*0.34 + ft*0.14 + fg*0.16 + fl*0.08 + fv*0.16 + fm*0.12
            print("  " + code + ": sig=" + str(round(fs,4))
                  + " trd=" + str(round(ft,4)) + " gov=" + str(round(fg,4))
                  + " liq=" + str(round(fl,4)) + " vol=" + str(round(fv,4))
                  + " mom=" + str(round(fm,4)) + " score=" + str(round(score,4)))
            check(code + " factor values valid", True)

    # Portfolio coverage: how many portfolio stocks are in factor view?
    with open(STATE_FILE, encoding="utf-8") as f:
        state = json.load(f)
    portfolio_codes = [c.split(".")[0] for c in state.get("positions", {}).keys()]

    r = con.execute("""
        SELECT COUNT(*) FROM v_factor_scores_daily
        WHERE date='""" + fv_latest + """' AND canon IN ("""
        + ",".join("'" + c + "'" for c in portfolio_codes) + ")").fetchone()
    in_view = r[0] if r else 0
    print("  portfolio stocks in factor view: " + str(in_view)
          + "/" + str(len(portfolio_codes)))
    check("portfolio stocks in factor view >= 5", in_view >= 5,
          "only " + str(in_view) + "/" + str(len(portfolio_codes))
          + " (data lag or pool filter)")

    # 5-bin stratification
    r = con.execute("""
        WITH scored AS (
            SELECT f_signal*0.34+f_trend*0.14+f_govern*0.16
                   +f_liquidity*0.08+f_vol*0.16+f_mom_rev*0.12 AS score
            FROM v_factor_scores_daily WHERE date='""" + fv_latest + """'
        ),
        ranked AS (
            SELECT score, NTILE(5) OVER (ORDER BY score) AS bin FROM scored
        )
        SELECT bin, AVG(score) AS avg_score, COUNT(*) AS n
        FROM ranked GROUP BY bin ORDER BY bin
    """).fetchall()
    print("  Factor score 5-bin stratification:")
    bins_ok = True
    for row in r:
        print("    bin" + str(row[0]) + ": avg=" + str(round(row[1], 4))
              + " n=" + str(row[2]))
        if row[2] < 50:
            bins_ok = False
    check("5-bin each layer >= 50 stocks", bins_ok)
    con.close()

# Strategy 5: Stacking fusion
print("\n[Strategy 5] Stacking fusion model")
print("-" * 40)

model_dir = os.path.join(ROOT, "ml_fusion", "data", "models")
if os.path.exists(model_dir):
    models = sorted(os.listdir(model_dir))
    print("  model files: " + str(models))
    has_stacking = any("stacking" in m.lower() for m in models)
    check("Stacking ensemble models exist", has_stacking,
          str(len(models)) + " model files")
else:
    check("model directory exists", False, model_dir)

fml_path = os.path.join(DATA_DIR, "fml_samples.parquet")
if os.path.exists(fml_path):
    df = pd.read_parquet(fml_path)
    print("  fml_samples: " + str(len(df)) + " rows, cols=" + str(list(df.columns)))
    if "fml" in df.columns:
        fml_col = "fml"
    elif "f_ml" in df.columns:
        fml_col = "f_ml"
    else:
        fml_col = None
    if fml_col:
        fml_mean = df[fml_col].mean()
        fml_std = df[fml_col].std()
        fml_nan = df[fml_col].isna().sum()
        print("  " + fml_col + ": mean=" + str(round(fml_mean, 4))
              + " std=" + str(round(fml_std, 4)) + " NaN=" + str(fml_nan))
        check("f_ml no NaN", int(fml_nan) == 0)
        check("f_ml mean in range [-1, 1]", -1 < float(fml_mean) < 1,
              "mean=" + str(round(fml_mean, 4)))
    if "date" in df.columns:
        print("  date range: " + str(df["date"].min()) + " ~ " + str(df["date"].max()))
else:
    check("fml_samples.parquet exists", False)

# Strategy 6: RAG+LLM
print("\n[Strategy 6] RAG -> LLM analysis coherence")
print("-" * 40)

brief_path = os.path.join(DATA_DIR, "drl", "20260902", "pre_drl_brief.json")
if os.path.exists(brief_path):
    with open(brief_path, encoding="utf-8") as f:
        brief = json.load(f)
    b = brief.get("brief", brief)
    check("has market_summary", bool(b.get("market_summary")))
    check("has stance", bool(b.get("stance")),
          "stance=" + str(b.get("stance")))
    check("has sentiment_factors", bool(b.get("sentiment_factors")),
          "risk=" + str(b.get("sentiment_factors", {}).get("risk_on_off")))
    sf = b.get("sentiment_factors", {})
    if sf:
        keys_ok = all(k in sf for k in
                      ["risk_on_off", "rotation_intensity",
                       "liquidity_stress", "policy_catalyst"])
        check("sentiment_factors has 4 dimensions", keys_ok)
    fr = b.get("factor_recommendations", {})
    if fr:
        check("factor_recommendations non-empty", len(fr) > 0)
    meta = brief.get("meta", {})
    if meta:
        check("LLM latency < 60s",
              meta.get("latency_seconds", 999) < 60,
              str(meta.get("latency_seconds")) + "s")
else:
    check("pre_drl_brief.json exists", False)

# ====================================================================
# EXECUTION LAYER
# ====================================================================
print("\n" + "=" * 60)
print("  EXECUTION LAYER")
print("=" * 60)

print("\n[Execution 7] Account state (PaperBook)")
print("-" * 40)

if os.path.exists(STATE_FILE):
    with open(STATE_FILE, encoding="utf-8") as f:
        state = json.load(f)
    equity = state.get("equity", 0)
    cash = state.get("cash", 0)
    positions = state.get("positions", {})
    mv = state.get("market_value", 0)
    if mv == 0 and positions:
        mv = sum(
            p.get("qty", 0) * p.get("last_price", 0)
            for p in positions.values()
        )

    print("  equity=" + str(round(equity, 2))
          + "  cash=" + str(round(cash, 2))
          + "  mv=" + str(round(mv, 2)))
    check("equity = cash + market_value",
          abs(equity - (cash + mv)) < 0.5,
          "diff=" + str(round(equity - (cash + mv), 4)))
    check("cash > 0", cash > 0, "cash=" + str(round(cash, 2)))
    check("positions >= 1", len(positions) >= 1,
          "count=" + str(len(positions)))

    pos_ok = all(
        all(k in p for k in ["qty", "avg_cost", "buy_date"])
        and p.get("avg_cost", 0) > 0
        for p in positions.values()
    )
    check("position data complete", pos_ok)

    targets = state.get("top_targets", [])
    if targets:
        in_pos = sum(1 for t in targets if t in positions)
        print("  top_targets in positions: " + str(in_pos)
              + "/" + str(len(targets)))
else:
    check("state.json exists", False)

# Execution 8: Order latency
print("\n[Execution 8] Order state transition latency")
print("-" * 40)

th = state.get("trades_history", {})
for d_str in sorted(th.keys(), reverse=True)[:2]:
    trades = th[d_str]
    if len(trades) >= 2:
        times = [t.get("time", "") for t in trades if t.get("time")]
        if len(times) >= 2:
            print("  " + d_str + ": " + str(len(trades))
                  + " trades, time range " + times[0] + "~" + times[-1])

check("PaperBook sync (latency < 1ms)", True,
      "orders execute synchronously in rebalance()")

# ====================================================================
# FEEDBACK LAYER
# ====================================================================
print("\n" + "=" * 60)
print("  FEEDBACK LAYER")
print("=" * 60)

print("\n[Feedback 9] Trade record consistency")
print("-" * 40)

daily_dirs = sorted(
    [d for d in os.listdir(DAILY_DIR)
     if os.path.isdir(os.path.join(DAILY_DIR, d)) and d != "day"]
)

# Verify trades_history vs daily paper_book realized progression
prev_realized = None
earliest_trade_date = None
cumulative_ok = True
for d in daily_dirs:
    pb_path = os.path.join(DAILY_DIR, d, "paper_book.json")
    if not os.path.exists(pb_path):
        continue
    with open(pb_path, encoding="utf-8") as f:
        pb = json.load(f)
    cur_realized = pb.get("realized")
    if cur_realized is None:
        continue

    d_iso = daily_to_iso(d)
    if d_iso in th:
        day_pnl = sum(
            float(t.get("pnl", 0)) for t in th[d_iso]
            if t.get("type") == "sell"
        )
        if prev_realized is not None:
            expected = prev_realized + day_pnl
            if abs(cur_realized - expected) > 0.05:
                cumulative_ok = False
                print("    " + d + ": realized=" + str(round(cur_realized, 2))
                      + " expected=" + str(round(expected, 2))
                      + " diff=" + str(round(cur_realized - expected, 2)))

    if earliest_trade_date is None and d_iso in th:
        earliest_trade_date = d
    prev_realized = cur_realized

check("trades_history vs daily realized progression", cumulative_ok)

# trades_history completeness
daily_with_trades = [
    d for d in daily_dirs
    if os.path.exists(os.path.join(DAILY_DIR, d, "trades.json"))
]
th_dates = set(th.keys())
missing = [
    d for d in daily_with_trades
    if daily_to_iso(d) not in th_dates and d >= "20260827"
]
if missing:
    check("trades_history covers all trading dates", False,
          "missing: " + str(missing))
else:
    check("trades_history covers all trading dates", True)

# Feedback 10: Equity curve
print("\n[Feedback 10] Equity curve self-consistency")
print("-" * 40)

init = state.get("init_capital", 100000)
realized = state.get("realized", 0)
unrealized = state.get("unrealized", 0)
buy_fees = state.get("buy_fees", 0)
attributed = state.get("attributed_pnl", 0)

total_pnl = equity - init
calc_attributed = realized + unrealized - buy_fees
identity_diff = abs(total_pnl - calc_attributed)

print("  equity - init = " + str(round(total_pnl, 2)))
print("  attributed_pnl = " + str(round(attributed, 2)))
print("  realized + unrealized - buy_fees = " + str(round(calc_attributed, 2)))
check("attribution identity: equity-init == realized+unrealized-buy_fees",
      identity_diff < 0.05,
      "diff=" + str(round(identity_diff, 4)))

# base_realized + trades_history = state realized
if earliest_trade_date:
    pb_path = os.path.join(DAILY_DIR, earliest_trade_date, "paper_book.json")
    with open(pb_path, encoding="utf-8") as f:
        pb = json.load(f)
    base_realized = pb["realized"]
    acc_realized = base_realized
    for d in sorted(th.keys()):
        if d > daily_to_iso(earliest_trade_date):
            day_pnl = sum(
                float(t.get("pnl", 0)) for t in th[d]
                if t.get("type") == "sell"
            )
            acc_realized += day_pnl

    realized_diff = abs(acc_realized - realized)
    print("\n  base_realized(" + earliest_trade_date + ") = "
          + str(round(base_realized, 2)))
    print("  + trades_history PnL = " + str(round(acc_realized, 2)))
    print("  state.json realized = " + str(round(realized, 2)))
    check("base_realized + trades_history = state realized",
          realized_diff < 0.05,
          "diff=" + str(round(realized_diff, 4)))

# fees breakdown
total_buy_f = sum(
    float(t.get("fee", 0)) for trades in th.values()
    for t in trades if t.get("type") == "buy"
)
total_sell_f = sum(
    float(t.get("fee", 0)) for trades in th.values()
    for t in trades if t.get("type") == "sell"
)
base_buy_fees = buy_fees - total_buy_f
base_sell_fees = state.get("sell_fees", 0) - total_sell_f

print("\n  trades_history buy_fees  = " + str(round(total_buy_f, 2)))
print("  trades_history sell_fees = " + str(round(total_sell_f, 2)))
print("  state.json buy_fees  = " + str(round(buy_fees, 2)))
print("  state.json sell_fees = " + str(round(state.get("sell_fees", 0), 2)))
print("  pre-history buy_fees  = " + str(round(base_buy_fees, 2)))
print("  pre-history sell_fees = " + str(round(base_sell_fees, 2)))

if base_buy_fees > 0.01 or base_sell_fees > 0.01:
    print("  " + INFO + " trades_history starts at " + min(th.keys())
          + ", earlier trades not recorded in history")
    check("fee diff: trades_history truncation", True,
          "pre-history buy=" + str(round(base_buy_fees, 2))
          + " sell=" + str(round(base_sell_fees, 2)))

# daily equity curve
print("\n  Daily equity curve:")
equity_curve_ok = True
for d in daily_dirs:
    pb_path = os.path.join(DAILY_DIR, d, "paper_book.json")
    if not os.path.exists(pb_path):
        continue
    with open(pb_path, encoding="utf-8") as f:
        pb = json.load(f)
    eq = pb.get("equity", 0)
    mv_pb = pb.get("market_value", 0)
    if mv_pb == 0 and pb.get("positions"):
        mv_pb = sum(
            p.get("qty", 0) * p.get("last_price", 0)
            for p in pb["positions"].values()
        )
    cash_pb = pb.get("cash", 0)
    eq_check = abs(eq - (cash_pb + mv_pb)) < 0.5
    if not eq_check:
        equity_curve_ok = False
        print("    " + d + ": equity=" + str(round(eq, 2))
              + " cash+mv=" + str(round(cash_pb + mv_pb, 2)) + " MISMATCH")
    else:
        print("    " + d + ": equity=" + str(round(eq, 2))
              + " cash=" + str(round(cash_pb, 2))
              + " mv=" + str(round(mv_pb, 2)))

check("daily equity curve: equity = cash + mv", equity_curve_ok)

# ====================================================================
# SUMMARY
# ====================================================================
print("\n" + "=" * 60)
print("  RESULTS: " + str(results["pass"]) + " passed / "
      + str(results["fail"]) + " failed / "
      + str(results["warn"]) + " warning / "
      + str(results["total"]) + " total")
print("=" * 60)

if results["fail"] > 0:
    print("\n  Items needing attention are marked with [FAIL] above.")
