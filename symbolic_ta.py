# ============================================================
# symbolic_ta.py -- 神经符号化趋势分析 (NeSy-TA) 的符号层
# ============================================================

import datetime as dt
import os
import json
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DUCKDB_PATH, DATA_DIR

def _log(msg: str) -> None:
    ts = dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] [sym_ta] {msg}', flush=True)

def _norm_date(d):
    s = str(d).strip()
    if len(s) == 8 and s.isdigit():
        return f'{s[:4]}-{s[4:6]}-{s[6:8]}'
    return s[:10]

def _ma_alignment(close):
    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    ma120 = close.rolling(120).mean()
    latest = len(close) - 1
    c = close.iloc[latest]
    m5, m20v, m60v, m120v = ma5.iloc[latest], ma20.iloc[latest], ma60.iloc[latest], ma120.iloc[latest]
    if pd.isna(m120v):
        return {'alignment': 'unknown', 'score': 0.0}
    if c > m5 > m20v > m60v > m120v:
        return {'alignment': 'bullish_full', 'score': 1.0}
    if c > m20v > m60v > m120v:
        return {'alignment': 'bullish_3', 'score': 0.7}
    if c > m20v > m60v:
        return {'alignment': 'bullish_2', 'score': 0.4}
    if c < m5 < m20v < m60v < m120v:
        return {'alignment': 'bearish_full', 'score': -1.0}
    if c < m20v < m60v < m120v:
        return {'alignment': 'bearish_3', 'score': -0.7}
    if c < m20v < m60v:
        return {'alignment': 'bearish_2', 'score': -0.4}
    return {'alignment': 'ranging', 'score': 0.0}

def _support_resistance(high, low, close):
    latest = len(close) - 1
    c = close.iloc[latest]
    h20 = high.iloc[max(0, latest - 19):latest + 1].max()
    l20 = low.iloc[max(0, latest - 19):latest + 1].min()
    ma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    upper = ma20 + 2 * std20
    lower = ma20 - 2 * std20
    bb_pos = 0.5
    if not pd.isna(upper.iloc[latest]) and (upper.iloc[latest] - lower.iloc[latest]) > 0:
        bb_pos = (c - lower.iloc[latest]) / (upper.iloc[latest] - lower.iloc[latest])
        bb_pos = max(0.0, min(1.0, bb_pos))
    range_pos = 0.5
    if h20 > l20:
        range_pos = (c - l20) / (h20 - l20)
        range_pos = max(0.0, min(1.0, range_pos))
    if c >= h20 * 0.995:
        breakout = 'resistance_break'
    elif c <= l20 * 1.005:
        breakout = 'support_break'
    else:
        breakout = 'inside_range'
    return {'bb_position': round(bb_pos, 3), 'range_position': round(range_pos, 3), 'breakout': breakout}

def _volume_price(close, volume):
    n = len(close)
    if n < 20:
        return {'state': 'unknown', 'score': 0.0}
    p5 = close.iloc[-5:].values
    v5 = volume.iloc[-5:].values
    v20_avg = volume.iloc[-20:].mean()
    pc = (p5[-1] - p5[0]) / p5[0] if p5[0] > 0 else 0.0
    vc = v5[-1] / v20_avg if v20_avg > 0 else 1.0
    if pc > 0.02 and vc > 1.3:
        return {'state': 'vol_breakout_bull', 'score': 0.6}
    if pc > 0.02 and vc < 0.8:
        return {'state': 'vol_divergence_bear', 'score': -0.3}
    if pc < -0.02 and vc > 1.3:
        return {'state': 'vol_breakout_bear', 'score': -0.6}
    if pc < -0.02 and vc < 0.8:
        return {'state': 'shrink_consolidate', 'score': 0.1}
    return {'state': 'vol_normal', 'score': 0.0}

def _volatility_regime(high, low, close):
    n = len(close)
    if n < 60:
        return {'regime': 'normal', 'atr_percentile': 0.5, 'score': 0.0}
    tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
    atr14 = tr.ewm(alpha=1.0 / 14, adjust=False).mean()
    latest_atr = atr14.iloc[-1]
    atr_60 = atr14.iloc[-60:]
    pct = (atr_60 < latest_atr).sum() / 60.0 if len(atr_60) == 60 else 0.5
    if pct > 0.8:
        return {'regime': 'high_vol', 'atr_percentile': round(pct, 3), 'score': -0.5}
    if pct < 0.2:
        return {'regime': 'low_vol', 'atr_percentile': round(pct, 3), 'score': 0.3}
    return {'regime': 'normal_vol', 'atr_percentile': round(pct, 3), 'score': 0.0}

def compute_symbolic_state(day, db_path=DUCKDB_PATH, panel=None):
    import duckdb
    day = _norm_date(day)
    con = duckdb.connect(db_path, read_only=True)
    try:
        cutoff = (dt.datetime.strptime(day, '%Y-%m-%d') - dt.timedelta(days=150)).strftime('%Y-%m-%d')
        df = con.execute("""
            SELECT b.symbol, b.date, b.close, b.high, b.low, b.volume, b.amount,
                   b.change_pct, b.turnover, s.market
            FROM daily_bars b
            LEFT JOIN symbols s ON s.symbol = b.symbol
            WHERE b.date BETWEEN ? AND ? AND b.close > 0 AND b.volume > 0
            ORDER BY b.symbol, b.date
        """, [cutoff, day]).fetchdf()
    finally:
        con.close()
    if df.empty:
        return {'day': day, 'ok': False, 'error': '无数据'}
    df['date'] = pd.to_datetime(df['date'])
    symbols = df['symbol'].unique()
    n_stocks = len(symbols)
    ma_scores, sr_positions, vp_scores, vol_pcts = [], [], [], []
    sample_n = min(500, n_stocks)
    rng = np.random.default_rng(42)
    sample_symbols = rng.choice(symbols, size=sample_n, replace=False)
    for sym in sample_symbols:
        sub = df[df['symbol'] == sym].sort_values('date')
        if len(sub) < 60:
            continue
        c = sub['close'].astype(float)
        h = sub['high'].astype(float)
        l = sub['low'].astype(float)
        v = sub['volume'].astype(float)
        ma = _ma_alignment(c)
        ma_scores.append(ma['score'])
        sr = _support_resistance(h, l, c)
        sr_positions.append(sr['range_position'])
        vp = _volume_price(c, v)
        vp_scores.append(vp['score'])
        vol = _volatility_regime(h, l, c)
        vol_pcts.append(vol['atr_percentile'])
    avg_ma = float(np.mean(ma_scores)) if ma_scores else 0.0
    avg_sr = float(np.mean(sr_positions)) if sr_positions else 0.5
    avg_vp = float(np.mean(vp_scores)) if vp_scores else 0.0
    avg_vol = float(np.mean(vol_pcts)) if vol_pcts else 0.5
    ma_bull = float(np.mean(np.array(ma_scores) > 0.1)) if ma_scores else 0.0
    ma_bear = float(np.mean(np.array(ma_scores) < -0.1)) if ma_scores else 0.0
    composite = 0.40 * avg_ma + 0.20 * (avg_sr - 0.5) * 2 + 0.20 * avg_vp + 0.20 * (0.5 - avg_vol)
    if composite > 0.25:
        ms = 'bull'
    elif composite < -0.25:
        ms = 'bear'
    else:
        ms = 'ranging'
    a = abs(composite)
    if a > 0.5:
        intens = 'strong'
    elif a > 0.25:
        intens = 'moderate'
    else:
        intens = 'weak'
    cons = max(ma_bull, ma_bear, 1 - ma_bull - ma_bear)
    conf = round(cons * 0.6 + (1.0 - np.std(ma_scores) / 2.0) * 0.4, 3) if ma_scores else 0.5
    conf = max(0.1, min(0.99, conf))
    features = {
        'f_ma_score': round(avg_ma, 4),
        'f_ma_bull_ratio': round(ma_bull, 4),
        'f_ma_bear_ratio': round(ma_bear, 4),
        'f_sr_position': round(avg_sr, 4),
        'f_vp_score': round(avg_vp, 4),
        'f_vol_percentile': round(avg_vol, 4),
        'f_composite': round(composite, 4),
        'f_ma_consensus': round(cons, 4),
        'f_bull_strength': round(ma_bull * (0.5 + 0.5 * max(0, avg_ma)), 4),
        'f_bear_strength': round(ma_bear * (0.5 + 0.5 * max(0, -avg_ma)), 4),
        'f_trend_persistence': round(avg_ma * (1.0 - avg_vol), 4),
        'f_regime_stability': round(1.0 - np.std(ma_scores) / 2.0, 4) if len(ma_scores) > 1 else 0.5,
    }
    return {
        'day': day, 'ok': True,
        'market_state': ms, 'intensity': intens, 'confidence': conf,
        'composite_score': round(composite, 4),
        'features': features,
        'detail': {
            'ma': {'avg_score': round(avg_ma, 4), 'bull_ratio': round(ma_bull, 4),
                   'bear_ratio': round(ma_bear, 4), 'sample_size': len(ma_scores)},
            'sr': {'avg_position': round(avg_sr, 4), 'sample_size': len(sr_positions)},
            'volume_price': {'avg_score': round(avg_vp, 4), 'sample_size': len(vp_scores)},
            'volatility': {'avg_percentile': round(avg_vol, 4), 'sample_size': len(vol_pcts)},
        },
        'n_stocks_total': n_stocks, 'n_stocks_sampled': sample_n,
    }

def save_symbolic_state(result, day_dir):
    out_dir = os.path.join(DATA_DIR, 'drl', day_dir)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, 'symbolic_ta.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    _log(f'symbolic_ta saved: {out_path}')
    return out_path

def load_symbolic_state(day_dir):
    p = os.path.join(DATA_DIR, 'drl', day_dir, 'symbolic_ta.json')
    if not os.path.exists(p):
        return None
    with open(p, encoding='utf-8') as f:
        return json.load(f)

if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='NeSy-TA Symbolic Layer')
    ap.add_argument('day', nargs='?', help='YYYY-MM-DD; default=latest')
    args = ap.parse_args()
    if args.day:
        day = _norm_date(args.day)
    else:
        import duckdb
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        r = con.execute('SELECT MAX(date) FROM daily_bars').fetchone()
        con.close()
        day = r[0].isoformat() if hasattr(r[0], 'isoformat') else str(r[0])
    result = compute_symbolic_state(day)
    if result['ok']:
        day_dir = result['day'].replace('-', '')
        save_symbolic_state(result, day_dir)
        print(f"\n{day} symbolic state: {result['market_state']} ({result['intensity']}), conf={result['confidence']:.2f}")
        for k, v in result['features'].items():
            print(f'  {k}: {v}')
    else:
        print(f'Error: {result.get("error")}')
