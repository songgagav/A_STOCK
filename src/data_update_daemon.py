# -*- coding: utf-8 -*-
"""数据更新守护 (data_update_daemon).

按数据类型就绪窗口, 在交易日盘后自动补齐缺口 (与 daemon/gate_refresh_daemon
并行常驻). 每次执行都先做"滞后巡检", 仅对滞后的数据执行补拉, 天然幂等
(重复触发/重复日期由目标脚本或 h5i 单调追加跳过).

窗口 (参考数据商典型更新时刻, 均保守延后避免源未就绪):
  15:45  日线缺口回补 (新浪快照; 针对缺失交易日)
  16:40  当日估值快照 (东财; 东财不可达时失败并记录, 下轮/次日重试)
  16:45  北向资金 (northbound_sync, 若滞后)
  17:20  两融 (margin_sync, 若滞后且通道可用)
  19:10  收盘主流程由主 daemon 触发 (见 daemon.py, 已后移至 19:10)

用法:  python src/data_update_daemon.py             # 常驻
       python src/data_update_daemon.py --once      # 立即执行一轮
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, date, timedelta

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(_BASE, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
os.chdir(_BASE)

PY = sys.executable
MARK = os.path.join(_BASE, "data", "data_update_last.json")
INTERVAL_S = int(os.environ.get("DATA_UPDATE_INTERVAL_S", "300"))
# 各阶段开始分钟 (当日 HH:MM 换算)
T_DAILY = 15 * 60 + 45
T_VALU = 16 * 60 + 40
T_NB = 16 * 60 + 45
T_MF = 16 * 60 + 50          # 板块资金流快照
T_MARGIN = 17 * 60 + 20      # 两融 (SSE 当日可见; 深/京 T+1 由次日运行补齐)
T_CLOSE_HINT = 19 * 60 + 10
# 阶段失败后的重试节流(秒)
FAIL_THROTTLE_S = int(os.environ.get("DATA_UPDATE_FAIL_THROTTLE_S", "1800"))


def _log(msg: str) -> None:
    print(f"[data_update] {datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def _now_min() -> int:
    n = datetime.now()
    return n.hour * 60 + n.minute


def _trade_days_range(lo: date, hi: date) -> list[date]:
    try:
        import factor_fusion as ff
        df = ff._sql(f"SELECT DISTINCT CAST(ts AS DATE) d FROM daily_bars "
                     f"WHERE CAST(ts AS DATE) >= DATE '{lo}' AND CAST(ts AS DATE) <= DATE '{hi}' "
                     f"ORDER BY d")
        return [r.d.date() if hasattr(r.d, "date") else r.d for r in df.itertuples()]
    except Exception:
        return []


def _daily_missing() -> list[str]:
    """需要回补的交易日 (existing max 之后, 且该日已过数据就绪窗口).

    当日(today)仅当 now>=T_DAILY(收盘数据就绪后)才纳入候选; 否则只回补到
    daily_bars 中已有的最近交易日为止, 避免把盘前快照误写成当日.
    """
    import factor_fusion as ff
    try:
        mx = ff._sql("SELECT MAX(CAST(ts AS DATE)) d FROM daily_bars").iloc[0, 0]
    except Exception:
        return []
    today = date.today()
    if _now_min() >= T_DAILY:
        hi = today
    else:
        # 取库中最近一个 <= today 的交易日
        df = ff._sql(f"SELECT DISTINCT CAST(ts AS DATE) d FROM daily_bars "
                     f"WHERE CAST(ts AS DATE) <= DATE '{today}' ORDER BY d DESC LIMIT 1")
        hi = df.iloc[0, 0].date() if len(df) else today
    days = _trade_days_range(mx + timedelta(days=1), hi)
    return [str(d) for d in days]


def _valu_stale_day() -> str | None:
    """valuation_snapshot 滞后时返回应补的交易日."""
    import factor_fusion as ff
    try:
        mx = ff._sql("SELECT MAX(CAST(ts AS DATE)) d FROM valuation_snapshot").iloc[0, 0]
        base = ff._sql("SELECT MAX(CAST(ts AS DATE)) d FROM daily_bars").iloc[0, 0]
    except Exception:
        return None
    if base is None or mx is None or mx >= base:
        return None
    days = _trade_days_range(mx + timedelta(days=1), base)
    return str(days[-1]) if days else None


def _state() -> dict:
    try:
        with open(MARK, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(st: dict) -> None:
    os.makedirs(os.path.dirname(MARK), exist_ok=True)
    with open(MARK, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def _run_once(stage: str, args: list[str], day: str | None = None) -> dict:
    cmd = [PY, *args]
    if day:
        cmd += ["--day", day]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1500)
        return {"ok": r.returncode == 0, "stage": stage, "day": day,
                "out": (r.stdout or "")[-2000:], "err": (r.stderr or "")[-300:]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "stage": stage, "day": day, "err": str(e)[:200]}


# 收盘后窗口与盘前窗口 (分钟, 用于估值/两融: 源仅在数据商发布后才可及)
_EVENING = (16 * 60, 22 * 60 + 30)
_MORNING = (6 * 60 + 30, 9 * 60)


def _in_publish_windows() -> bool:
    m = _now_min()
    return any(lo <= m <= hi for lo, hi in (_EVENING, _MORNING))


def _due(st: dict, stage: str, force: bool) -> bool:
    """失败节流: 距上次失败 >= FAIL_THROTTLE_S 才允许重试."""
    if force:
        return True
    lf = (st.get("fail") or {}).get(stage)
    if not lf:
        return True
    try:
        last = datetime.fromisoformat(lf)
        return (datetime.now() - last).total_seconds() >= FAIL_THROTTLE_S
    except Exception:
        return True


def _mark_fail(st: dict, stage: str) -> None:
    st.setdefault("fail", {})[stage] = datetime.now().isoformat()


def run_round(force: bool = False) -> dict:
    st = _state()
    today = datetime.now().strftime("%Y-%m-%d")
    t0 = _now_min()
    out = {}

    def after(T: int) -> bool:
        return force or t0 >= T

    # 1) 日线缺口回补 (15:45 后; 每缺口日一次)
    if after(T_DAILY):
        for d in _daily_missing():
            key = f"daily:{d}"
            if st.get(key) == today and not force:
                continue
            out[key] = _run_once("daily", ["scripts/backfill_daily.py"], day=d)
            if out[key]["ok"]:
                st[key] = today
            else:
                _mark_fail(st, "daily")
    # 2) 估值快照 (16:40 后; 东财全市场接口 2026-09-08 起被反爬断连, 改用腾讯 qt.gtimg 源)
    if after(T_VALU):
        d = _valu_stale_day()
        if d:
            key = f"valuation:{d}"
            if (st.get(key) != today or force) and _due(st, "valuation", force):
                out[key] = _run_once("valuation", ["scripts/tencent_valuation.py"], day=d)
                if out[key]["ok"]:
                    st[key] = today
                    (st.get("fail") or {}).pop("valuation", None)
                else:
                    _mark_fail(st, "valuation")
    # 3) 板块资金流快照 (16:50 后; 同日幂等, 失败节流重试)
    if after(T_MF) and _due(st, "money_flow", force):
        out["money_flow"] = _run_once("money_flow", ["src/money_flow_sync.py", "sync"])
        if out["money_flow"]["ok"]:
            (st.get("fail") or {}).pop("money_flow", None)
        else:
            _mark_fail(st, "money_flow")
    # 4) 两融 (17:20 后且处于发布窗口; margin_sync 自带幂等/水位; 深京 T+1 次日自愈)
    if after(T_MARGIN) and (force or _in_publish_windows()) and _due(st, "margin", force):
        out["margin"] = _run_once("margin", ["src/margin_sync.py", "sync", "--days", "2"])
        _o = out["margin"].get("out") or ""
        # 成功判定: 退出码 0 且无缺腿(缺腿=SSE/深/京当日未发布, 视为未完成以便节流续试)
        incomplete = '"missing_legs": {' not in _o or '"missing_legs": {}' in _o
        if out["margin"]["ok"] and incomplete:
            (st.get("fail") or {}).pop("margin", None)
        else:
            _mark_fail(st, "margin")
    if force or t0 >= T_CLOSE_HINT:
        out["_note"] = "收盘主流程由 daemon.py 在收盘窗口执行 (见 daemon.py)"
    _save(st)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    if args.once:
        rep = run_round(force=True)
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
        return
    _log(f"启动 (interval={INTERVAL_S}s) 窗口: 日线15:45 / 估值16:40 / 收盘由主daemon")
    while True:
        try:
            rep = run_round()
            for k, v in rep.items():
                if isinstance(v, dict) and "ok" in v:
                    tail = (v.get("out") or v.get("err") or "")[:90]
                    _log(f"{k}: ok={v['ok']} ({tail})")
        except Exception as e:  # noqa: BLE001
            _log(f"轮询异常: {e}")
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    main()
